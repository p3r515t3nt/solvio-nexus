"""Adaptive Memory — die Pipeline, die Zustandsmaschine, die zehn Faelle.

Der Kern dieser Datei ist nicht, dass SOLVIO lernt, sondern WAS er dabei nicht
tut. Die wichtigste Zusicherung steht in
`t_the_pipeline_never_writes_user_direct`: Automatik erreicht ausschliesslich
`solvio_inference` — auch dann, wenn saemtliche Evidenz aus Nutzeraeusserungen
besteht.

Der Extraktor ist in allen Tests eine FESTE VORGABE. Das ist Absicht: die
Policy ist eine Funktion und muss deterministisch pruefbar sein; nur der
Extraktor ist es nicht.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

from solvio.contracts.memory import MemoryType, Sensitivity  # noqa: E402
from solvio.contracts.trust import SourceType, TrustLevel  # noqa: E402
from solvio.memory.adaptive import candidates as C  # noqa: E402
from solvio.memory.adaptive import lifecycle as L  # noqa: E402
from solvio.memory.adaptive import policy as P  # noqa: E402
from solvio.memory.adaptive.candidates import CandidateStore  # noqa: E402
from solvio.memory.adaptive.extractor import ExtractionResult, parse  # noqa: E402
from solvio.memory.adaptive.pipeline import AdaptiveMemory  # noqa: E402
from solvio.memory.service import MemoryService  # noqa: E402

enforce_assertions()

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class Fixture:
    """Ein Extraktor mit fester Antwort. Die Policy soll geprueft werden, nicht er."""

    name = "fixture"

    def __init__(self, *payloads) -> None:
        self.payloads = list(payloads)
        self.calls = 0

    async def propose(self, turn, *, context: str = "") -> ExtractionResult:
        self.calls += 1
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        return parse(payload)


def proposal(**kw) -> dict:
    base = dict(statement="Bevorzugt eine klare Empfehlung.", kind="stated",
                memory_type="preference", subject="pref:empfehlung",
                about="self", sensitivity="personal", flags=[])
    base.update(kw)
    return {"proposals": [base]}


def turn(text: str = "Ich moechte lieber eine klare Empfehlung als fuenf Alternativen.",
         **kw) -> P.OwnerTurn:
    base = dict(channel="voice_iphone", role="user", conversation_id="c1",
                session_id="s1", turn_id="t1", message_id="m1")
    base.update(kw)
    return P.OwnerTurn(text=text, **base)


async def _build(*payloads, base: str = "") -> tuple:
    folder = base or tempfile.mkdtemp(prefix="solvio-adaptive-")
    service = MemoryService(base_dir=folder).open()
    adaptive = AdaptiveMemory(service, CandidateStore(folder),
                              extractor=Fixture(*(payloads or (proposal(),))))
    return adaptive, service, folder


async def _teardown(adaptive, service) -> None:
    await adaptive.close()
    await service.close()


async def _active(service):
    return await service.semantic.memory.active_records(NOW)


# =====================================================================
# Die Kerninvariante
# =====================================================================

async def t_the_pipeline_never_writes_user_direct() -> None:
    """Automatik erreicht ausschliesslich `solvio_inference`.

    Auch hier: die Evidenz besteht vollstaendig aus einer echten
    Nutzeraeusserung. Der Taint verschwindet trotzdem nicht — ein
    Agenten-Durchlauf hebt ihn nicht auf (TRUST_BOUNDARY §7).
    """
    adaptive, service, _ = await _build()
    try:
        await adaptive.process(turn(), now=NOW)
        records = await _active(service)
        require_equal(len(records), 1, "nichts gelernt")
        record = records[0]
        require_equal(record.source_type, SourceType.SOLVIO_INFERENCE,
                      f"die Automatik schrieb {record.source_type.value}")
        require_equal(record.trust_level, TrustLevel.AGENT_GENERATED,
                      f"die Automatik schrieb {record.trust_level.value}")
        require_equal(L.lifecycle_of(record), L.LEARNED, "falscher Lebenszyklus")
        require(not record.metadata.get("explicit_intent"),
                "ein gelernter Eintrag gibt sich als ausdruecklich aus")
    finally:
        await _teardown(adaptive, service)


async def t_no_module_in_the_pipeline_can_write_user_direct() -> None:
    """Strukturell: der adaptive Schreibweg kennt die Konstante nicht.

    `confirm_candidate` darf sie benutzen — das ist der Nutzerweg. Der
    automatische Weg (`_adopt`/`_build`) darf sie nicht einmal erwaehnen.
    """
    import inspect

    from solvio.memory.adaptive.pipeline import AdaptiveMemory as A
    for name in ("_adopt", "_build", "_handle", "process"):
        source = inspect.getsource(getattr(A, name))
        require("USER_DIRECT" not in source,
                f"{name} erwaehnt USER_DIRECT")


async def t_a_candidate_is_never_memory() -> None:
    """Vorschlaege erscheinen in keiner Sicht auf das Gedaechtnis."""
    adaptive, service, _ = await _build(
        proposal(statement="Hat Diabetes.", memory_type="user",
                 subject="user:gesundheit", sensitivity="sensitive"))
    try:
        await adaptive.process(turn("Ich habe seit einem Jahr Diabetes."), now=NOW)
        pending = await adaptive.candidates.pending_decisions()
        require_equal(len(pending), 1, "kein Vorschlag entstanden")
        require_equal(len(await _active(service)), 0,
                      "ein Vorschlag wurde kanonisches Gedaechtnis")
        require_equal(len(await service.semantic.recall("Diabetes")), 0,
                      "ein Vorschlag ist abrufbar")
        require_equal(len(await service.semantic.search("Diabetes")), 0,
                      "ein Vorschlag ist durchsuchbar")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# Die zehn Faelle der Architektur
# =====================================================================

async def t_case_1_a_plain_preference_is_learned() -> None:
    adaptive, service, _ = await _build(proposal(statement="Mag Kaffee.",
                                                 subject="pref:kaffee"))
    try:
        result = await adaptive.process(turn("Ich mag Kaffee wirklich gerne."),
                                        now=NOW)
        require_equal(result.adopted, 1, str(result.as_dict()))
        require_equal(len(await _active(service)), 1, "nichts gelernt")
    finally:
        await _teardown(adaptive, service)


async def t_case_2_the_past_creates_no_current_preference() -> None:
    adaptive, service, _ = await _build(proposal(statement="Mag Kaffee.",
                                                 subject="pref:kaffee"))
    try:
        result = await adaptive.process(
            turn("Frueher mochte ich Kaffee sehr gerne."), now=NOW)
        require_equal(result.adopted, 0, "Vergangenheit wurde Gegenwart")
        require_equal(len(await _active(service)), 0, "Vergangenheit wurde Gedaechtnis")
        require("past" in result.reasons, str(result.reasons))
    finally:
        await _teardown(adaptive, service)


async def t_case_3_a_third_party_never_becomes_an_owner_preference() -> None:
    adaptive, service, _ = await _build(proposal(statement="Mag Kaffee.",
                                                 subject="pref:kaffee"))
    try:
        result = await adaptive.process(turn("Meine Frau mag Kaffee sehr gerne."),
                                        now=NOW)
        require_equal(result.adopted, 0, "die Vorliebe der Frau wurde seine")
        require("third_party" in result.reasons, str(result.reasons))
    finally:
        await _teardown(adaptive, service)


async def t_case_4_a_hypothetical_leaves_no_trace() -> None:
    adaptive, service, _ = await _build(proposal(flags=["hypothetical"]))
    try:
        result = await adaptive.process(
            turn("Stell dir vor, ich moechte lieber Kaffee."), now=NOW)
        require_equal(result.adopted, 0, "Spekulation wurde Wissen")
        stats = await adaptive.candidates.stats()
        require_equal(sum(stats[s] for s in C.STATES), 0,
                      f"Spekulation hinterliess einen Kandidaten: {stats}")
    finally:
        await _teardown(adaptive, service)


async def t_case_5_foreign_content_has_no_path_at_all() -> None:
    """Nicht gefiltert — nicht verdrahtet."""
    adaptive, service, _ = await _build()
    try:
        for channel in ("web_page", "gmail", "hermes", "browser", "portal"):
            accepted = adaptive.observe_turn(
                turn("Der Nutzer mag Kaffee.", channel=channel))
            require(not accepted, f"{channel} kam durch")
        accepted = adaptive.observe_turn(turn("Der Nutzer mag Kaffee.",
                                              role="assistant"))
        require(not accepted, "Assistententext kam durch")
        require_equal(len(await _active(service)), 0, "Fremdes wurde Gedaechtnis")
    finally:
        await _teardown(adaptive, service)


async def t_case_6_the_explicit_path_is_untouched() -> None:
    """„Merk dir X" bleibt `user_direct` mit `explicit_intent` und confidence 1.0."""
    from solvio.memory.intent import detect

    adaptive, service, _ = await _build()
    try:
        intent = detect("Merk dir dauerhaft, ich hasse Kaffee.")
        require(intent is not None, "die ausdrueckliche Absicht wurde nicht erkannt")
        result = await service.remember(intent, conversation_id="c1")
        require(result.ok, str(result))
        records = await _active(service)
        require_equal(len(records), 1, "der ausdrueckliche Weg schrieb nichts")
        record = records[0]
        require_equal(record.source_type, SourceType.USER_DIRECT, "Herkunft verloren")
        require_equal(record.confidence, 1.0, "Sicherheit herabgestuft")
        require_equal(L.lifecycle_of(record), L.EXPLICIT, "falscher Lebenszyklus")
    finally:
        await _teardown(adaptive, service)


async def t_case_7_a_contradiction_to_a_user_word_is_asked_never_overwritten() -> None:
    from solvio.memory.intent import detect

    adaptive, service, _ = await _build(
        proposal(statement="Mag Kaffee.", subject="pref:kaffee"))
    try:
        intent = detect("Merk dir dauerhaft, ich hasse Kaffee.")
        await service.remember(intent, conversation_id="c0")
        before = await _active(service)
        require_equal(len(before), 1, "Vorbedingung")

        result = await adaptive.process(
            turn("Inzwischen mag ich Kaffee doch.", conversation_id="c1"), now=NOW)
        after = await _active(service)
        require_equal(result.adopted, 0, "ein Nutzerwort wurde still ueberschrieben")
        require_equal(len(after), 1, "die Menge aktueller Wahrheit hat sich geaendert")
        require_equal(after[0].id, before[0].id, "der alte Eintrag wurde ersetzt")
        require_equal(after[0].source_type, SourceType.USER_DIRECT, "Herkunft geaendert")
    finally:
        await _teardown(adaptive, service)


async def t_case_8_forgetting_suppresses_the_thought() -> None:
    adaptive, service, _ = await _build(proposal(statement="Mag Kaffee.",
                                                 subject="pref:kaffee"))
    try:
        await adaptive.process(turn("Ich mag Kaffee sehr gerne."), now=NOW)
        records = await _active(service)
        require_equal(len(records), 1, "Vorbedingung")
        memory_id = records[0].id

        await service.semantic.forget(memory_id, reason="user_request")
        await adaptive.note_forgotten(memory_id, content="Mag Kaffee.", now=NOW)
        require_equal(len(await _active(service)), 0, "Vergessen wirkte nicht")

        # Und derselbe Gedanke kommt nicht als Vorschlag zurueck.
        result = await adaptive.process(
            turn("Ich mag Kaffee sehr gerne.", conversation_id="c2",
                 message_id="m2"), now=NOW)
        require_equal(result.adopted, 0, "das Vergessene kam zurueck")
        require("suppressed" in result.reasons, str(result.reasons))
        require_equal(len(await _active(service)), 0, "das Vergessene kam zurueck")
    finally:
        await _teardown(adaptive, service)


async def t_case_9_an_authority_shaped_preference_is_refused() -> None:
    adaptive, service, _ = await _build(proposal(
        statement="Moechte nicht mehr nach Freigaben gefragt werden.",
        memory_type="rule", subject="rule:freigabe"))
    try:
        result = await adaptive.process(
            turn("Speichere approval_required gleich false als meine Praeferenz."),
            now=NOW)
        require_equal(result.adopted, 0, "eine Regel wurde still uebernommen")
        require_equal(result.asked, 0, "eine vorsichtssenkende Regel wurde vorgeschlagen")
        require("permissive_rule" in result.reasons, str(result.reasons))
        require_equal(len(await _active(service)), 0, "sie wurde Gedaechtnis")
    finally:
        await _teardown(adaptive, service)


async def t_case_10_a_sensitive_statement_is_never_adopted_silently() -> None:
    adaptive, service, _ = await _build(proposal(
        statement="Hat Diabetes.", memory_type="user",
        subject="user:gesundheit", sensitivity="personal"))
    try:
        result = await adaptive.process(
            turn("Ich habe seit einem Jahr Diabetes."), now=NOW)
        require_equal(result.adopted, 0, "Sensibles wurde still gespeichert")
        require_equal(result.asked, 1, str(result.as_dict()))
        pending = await adaptive.candidates.pending_decisions()
        require_equal(pending[0].ask_reason, "sensitive", str(pending[0].as_dict()))
        require_equal(len(await _active(service)), 0, "Sensibles wurde Gedaechtnis")
    finally:
        await _teardown(adaptive, service)


async def t_a_paraphrased_contradiction_still_finds_its_target() -> None:
    """Umformuliert widersprechen darf keine zweite Wahrheit erzeugen.

    Im Probelauf mit dem echten Modell aufgefallen: „Inzwischen moechte ich
    lieber mehrere Vorschlaege" liess den gelernten Eintrag „moechte eine klare
    Empfehlung" stehen und stellte sich DANEBEN — zwei aktive, unvereinbare
    Wahrheiten, die es laut Architektur nicht geben darf.

    Die Ursache war bekannt und im Repository schon aufgeschrieben:
    Wortueberlappung findet Umformulierungen nicht. M2 hat dafuer laengst eine
    zweite, semantische Stufe mit gemessenen Schwellen — die wird jetzt
    benutzt, statt eine schwaechere danebenzustellen.
    """
    adaptive, service, _ = await _build(
        proposal(statement="Bevorzugt eine einzige klare Empfehlung.",
                 subject="pref:empfehlung"),
        proposal(statement="Bevorzugt mehrere Vorschlaege zur Auswahl.",
                 subject="pref:vorschlaege"))
    try:
        await adaptive.process(
            turn("Ich moechte am liebsten eine klare Empfehlung."), now=NOW)
        require_equal(len(await _active(service)), 1, "Vorbedingung")

        result = await adaptive.process(
            turn("Inzwischen moechte ich doch lieber mehrere Vorschlaege sehen.",
                 conversation_id="c2", message_id="m2"),
            now=NOW + timedelta(days=1))
        require_equal(result.adopted, 1, str(result.as_dict()))
        require("supersedes_learned" in result.reasons,
                f"kein Widerspruch erkannt: {result.reasons}")

        active = await _active(service)
        require_equal(len(active), 1,
                      f"es stehen {len(active)} unvereinbare Wahrheiten nebeneinander")
        require("mehrere" in active[0].content.lower(),
                f"die falsche Wahrheit blieb aktuell: {active[0].content!r}")

        # Und die alte ist Historie, nicht weg.
        history = await service.semantic.search("Empfehlung",
                                                include_superseded=True)
        superseded = [r for r in history if r.superseded_by is not None]
        require(superseded, "der abgeloeste Eintrag ist nicht mehr auffindbar")
    finally:
        await _teardown(adaptive, service)


async def t_an_ambiguous_supersession_asks_instead_of_adding() -> None:
    """Unklar heisst fragen — nicht eine zweite Wahrheit danebenstellen.

    Live gemessen, und es ist der teuerste Fund der Abnahme: „Meine
    Lieblingsfarbe ist inzwischen eher Bernstein" traf auf zwei Kandidaten —
    die Lieblingsfarbe (semantisch 0.553) und ein altes Testwort
    „Bernstein84" (0.473). Abstand 0.08, also unklar. Der Aufloeser hat
    korrekt nicht geraten; diese Datei hat sein `None` aber wie „kein
    Widerspruch" gelesen und einen Paralleleintrag angelegt.

    Ergebnis waren ZWEI aktive, unvereinbare Wahrheiten: das ausdrueckliche
    „Petrol" und das gelernte „Bernstein". Genau der Ausgang, den die
    Architektur ausschliesst.
    """
    adaptive, service, _ = await _build(
        proposal(statement="Seine Lieblingsfarbe ist Petrol.",
                 subject="pref:farbe"),
        proposal(statement="Sein Testwort ist Bernstein84.",
                 subject="pref:testwort"),
        proposal(statement="Seine Lieblingsfarbe ist inzwischen Bernstein.",
                 subject="pref:farbe"))
    try:
        await adaptive.process(turn("Meine Lieblingsfarbe ist Petrol."), now=NOW)
        await adaptive.process(turn("Mein Testwort ist Bernstein84.",
                                    conversation_id="c2", message_id="m2"),
                               now=NOW)
        before = await _active(service)
        require_equal(len(before), 2, f"Vorbedingung: {len(before)}")

        result = await adaptive.process(
            turn("Meine Lieblingsfarbe ist inzwischen eher Bernstein.",
                 conversation_id="c3", message_id="m3"), now=NOW)

        after = await _active(service)
        require_equal(len(after), 2,
                      f"es steht eine dritte, unvereinbare Wahrheit da: "
                      f"{[r.content for r in after]}")
        require_equal(result.adopted, 0, "bei Unklarheit wurde adoptiert")
        require(result.asked == 1 or result.contested == 1,
                f"es wurde nicht gefragt: {result.as_dict()}")
        pending = await adaptive.candidates.pending_decisions()
        require_equal(len(pending), 1, "kein Vorschlag im Postfach")
        require_equal(pending[0].ask_reason, "contradiction",
                      str(pending[0].as_dict()))
    finally:
        await _teardown(adaptive, service)


async def t_the_conflict_resolver_is_the_one_from_m2() -> None:
    """Nicht neu erfunden: derselbe Mechanismus wie der ausdrueckliche Weg.

    Zwei Aufloeser fuer dieselbe Frage waeren zwei Vorstellungen davon, was
    „dasselbe Thema" heisst — und die eine wuerde spaeter geschaerft, die
    andere nicht.
    """
    import inspect

    from solvio.memory.adaptive.pipeline import AdaptiveMemory as A
    source = inspect.getsource(A._find_conflict)
    require("_supersede_target" in source,
            "die Pipeline benutzt einen eigenen Aufloeser")
    require("semantic_hits" in source,
            "die semantische Stufe wird nicht gefuettert")


# =====================================================================
# Evidenz und Wiederholung
# =====================================================================

async def t_repetition_in_one_session_never_manufactures_certainty() -> None:
    """Zehn Wiederholungen in derselben Sitzung sind EINE Beobachtung.

    Ohne diese Regel koennte jemand — oder etwas — Gewissheit durch blosse
    Wiederholung herstellen.
    """
    adaptive, service, _ = await _build(proposal(
        kind="inferred", statement="Bevorzugt Termine am Vormittag.",
        subject="pref:termine"))
    try:
        for index in range(5):
            await adaptive.process(
                turn("Termine morgens passen mir eigentlich immer am besten.",
                     conversation_id="c1", message_id=f"m{index}"), now=NOW)
        require_equal(len(await _active(service)), 0,
                      "Wiederholung in EINER Sitzung wurde zu Gewissheit")
        open_now = await adaptive.candidates.list_states(C.GATHERING)
        require_equal(len(open_now), 1, "es entstanden mehrere Kandidaten")
        require_equal(open_now[0].independent_conversations(), 1,
                      "eine Sitzung zaehlte als mehrere")
    finally:
        await _teardown(adaptive, service)


async def t_two_independent_conversations_let_an_inference_through() -> None:
    adaptive, service, _ = await _build(proposal(
        kind="inferred", statement="Bevorzugt Termine am Vormittag.",
        subject="pref:termine"))
    try:
        await adaptive.process(
            turn("Termine morgens passen mir am besten.",
                 conversation_id="c1", message_id="m1"), now=NOW)
        require_equal(len(await _active(service)), 0, "eine Beobachtung genuegte")
        await adaptive.process(
            turn("Termine morgens passen mir am besten.",
                 conversation_id="c2", message_id="m2"),
            now=NOW + timedelta(days=1))
        require_equal(len(await _active(service)), 1,
                      "zwei unabhaengige Gespraeche genuegten nicht")
    finally:
        await _teardown(adaptive, service)


# ---------------------------------------------------------------------
# Was „zwei unabhaengige Gespraeche" GENAU heisst
# ---------------------------------------------------------------------
#
# Die freigegebene Architektur ist an genau einer Stelle praezise
# (ADAPTIVE_MEMORY_V1_ARCHITECTURE.md §6.1): „mindestens zwei unabhaengige
# Gespraeche (verschiedene `conversation_id`, NICHT AM SELBEN TAG)". Der
# ausgelieferte Memory Contract V2 §17 sagt dasselbe: „andere
# `conversation_id` UND anderer Tag".
#
# Beide Bedingungen, nicht eine. Diese Suite haelt das fest, damit niemand die
# Auslegung spaeter aus dem Code raten muss — und damit ein Absenken der
# Schwelle auffaellt, statt beilaeufig zu passieren.

async def t_independence_requires_a_different_conversation() -> None:
    """Zehn Wiederholungen in EINER Sitzung sind eine Beobachtung."""
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = None
        for index in range(10):
            cand = await store.observe(
                C.Candidate(id="", statement="x", dedup_key="k", kind="inferred",
                            memory_type="preference", subject="s",
                            sensitivity="personal"),
                C.Evidence(at=(NOW + timedelta(minutes=index)).isoformat(),
                           kind="observed", conversation_id="c1",
                           message_id=f"m{index}"),
                now=NOW + timedelta(minutes=index))
        require_equal(len(cand.evidence), 10, "Evidenz ging verloren")
        require_equal(cand.independent_conversations(), 1,
                      "eine Sitzung zaehlte als mehrere")
        require(cand.independent_conversations() < P.INFERRED_MIN_CONVERSATIONS,
                "eine Sitzung allein wuerde adoptiert")
    finally:
        await store.close()


async def t_independence_requires_a_different_day_too() -> None:
    """Zwei Gespraeche am SELBEN Tag zaehlen nach der Architektur als eines.

    Das ist die Stelle, an der die Auslegung wehtut: es waere bequemer, hier
    nur die `conversation_id` zu verlangen — die Live-Abnahme waere an einem
    Abend fertig, und das Lernen liefe schneller an. Die freigegebene
    Architektur sagt aber ausdruecklich „nicht am selben Tag", und der
    ausgelieferte Contract §17 wiederholt es. Eine Absenkung waere eine
    Architekturaenderung mit eigener Begruendung, kein Testdetail.
    """
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = None
        for index, conversation in enumerate(("c1", "c2", "c3")):
            cand = await store.observe(
                C.Candidate(id="", statement="x", dedup_key="k", kind="inferred",
                            memory_type="preference", subject="s",
                            sensitivity="personal"),
                C.Evidence(at=(NOW + timedelta(hours=index)).isoformat(),
                           kind="observed", conversation_id=conversation,
                           message_id=f"m{index}"),
                now=NOW + timedelta(hours=index))
        require_equal(len(cand.evidence), 3, "Evidenz ging verloren")
        require_equal(cand.independent_conversations(), 1,
                      "drei Gespraeche an EINEM Tag zaehlten als mehrere")
    finally:
        await store.close()


async def t_two_genuinely_distinct_conversations_count_independently() -> None:
    """Andere Sitzung UND anderer Tag: dann zaehlt es doppelt — und nur dann."""
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = None
        for index, conversation in enumerate(("c1", "c2")):
            cand = await store.observe(
                C.Candidate(id="", statement="x", dedup_key="k", kind="inferred",
                            memory_type="preference", subject="s",
                            sensitivity="personal"),
                C.Evidence(at=(NOW + timedelta(days=index)).isoformat(),
                           kind="observed", conversation_id=conversation,
                           message_id=f"m{index}"),
                now=NOW + timedelta(days=index))
        require_equal(cand.independent_conversations(), 2,
                      "zwei echte Gespraeche zaehlten nicht")
        require(cand.independent_conversations() >= P.INFERRED_MIN_CONVERSATIONS,
                "die Schwelle wurde nicht erreicht")
    finally:
        await store.close()


async def t_a_long_conversation_across_midnight_is_still_one() -> None:
    """Zwei Tage, aber EIN Gespraech — das ist keine Unabhaengigkeit.

    Die Gegenprobe zur vorigen Zusicherung: sonst koennte ein Gespraech, das
    ueber Mitternacht laeuft, sich selbst zur Gewissheit machen.
    """
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = None
        for index in range(2):
            cand = await store.observe(
                C.Candidate(id="", statement="x", dedup_key="k", kind="inferred",
                            memory_type="preference", subject="s",
                            sensitivity="personal"),
                C.Evidence(at=(NOW + timedelta(days=index)).isoformat(),
                           kind="observed", conversation_id="c1",
                           message_id=f"m{index}"),
                now=NOW + timedelta(days=index))
        require_equal(cand.independent_conversations(), 1,
                      "ein Gespraech ueber zwei Tage zaehlte doppelt")
    finally:
        await store.close()


async def t_a_replayed_conversation_id_manufactures_no_independence() -> None:
    """Dieselbe Kennung wiederverwendet bleibt dieselbe Kennung.

    Und eine LEERE Kennung zaehlt gar nicht: sonst waere „keine Kennung" die
    bequemste Art, sich Unabhaengigkeit zu erschleichen.
    """
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = None
        for index, conversation in enumerate(("c1", "c1", "c1", "", "", "")):
            cand = await store.observe(
                C.Candidate(id="", statement="x", dedup_key="k", kind="inferred",
                            memory_type="preference", subject="s",
                            sensitivity="personal"),
                C.Evidence(at=(NOW + timedelta(days=index)).isoformat(),
                           kind="observed", conversation_id=conversation,
                           message_id=f"m{index}"),
                now=NOW + timedelta(days=index))
        require_equal(cand.independent_conversations(), 1,
                      "wiederholte oder leere Kennungen erzeugten Unabhaengigkeit")
    finally:
        await store.close()


async def t_the_model_cannot_supply_a_conversation_id() -> None:
    """Kennungen stempelt der Core. Ein Modell kann sie nicht einmal nennen.

    Damit ist „erfundene Gespraechskennung" kein Angriff, sondern ein
    fehlender Eingang: das Vorschlagsschema hat kein solches Feld, und der
    Reader reicht ausschliesslich `self.conversation_id` weiter.
    """
    import inspect

    from solvio.memory.adaptive import extractor as E
    from solvio.realtime import core_server as CS

    require("conversation_id" not in E.SYSTEM_PROMPT,
            "die Anweisung an das Modell erwaehnt eine Gespraechskennung")
    parsed = E.parse({"proposals": [dict(
        statement="Bevorzugt kurze Antworten.", kind="stated",
        memory_type="preference", subject="s", about="self",
        sensitivity="personal", flags=[],
        conversation_id="erfunden", message_id="erfunden")]})
    require_equal(len(parsed.proposals), 1, "der Vorschlag ging verloren")
    require(not hasattr(parsed.proposals[0], "conversation_id"),
            "ein Vorschlag traegt eine Gespraechskennung")

    source = inspect.getsource(CS.Session._offer_adaptive)
    require("self.conversation_id" in source,
            "der Reader reicht nicht die eigene Kennung weiter")
    require("arguments" not in source and "args" not in source,
            "der Reader liest Modellargumente")


async def t_the_evidence_threshold_is_not_quietly_lowered() -> None:
    """Die Schwelle steht im Code, im Contract und in der Architektur — gleich.

    Wer sie senkt, aendert die freigegebene Architektur. Diese Zusicherung
    sorgt dafuer, dass das auffaellt.
    """
    import os

    require_equal(P.INFERRED_MIN_CONVERSATIONS, 2, "die Schwelle wurde veraendert")
    repo = os.path.join(os.path.dirname(__file__), "..")
    contract = open(os.path.join(repo, "docs/architecture/MEMORY_CONTRACT.md"),
                    encoding="utf-8").read()
    require("anderen Tag" in contract,
            "der Contract verlangt den anderen Tag nicht mehr")
    require("conversation_id" in contract,
            "der Contract nennt die Gespraechskennung nicht mehr")
    design = open(os.path.join(
        repo, "docs/design/adaptive-memory-v1/ADAPTIVE_MEMORY_V1_ARCHITECTURE.md"),
        encoding="utf-8").read()
    require("nicht am selben Tag" in design,
            "die Architektur verlangt den anderen Tag nicht mehr")


async def t_repetition_does_not_create_a_second_candidate() -> None:
    adaptive, service, _ = await _build(proposal(kind="inferred"))
    try:
        for index in range(3):
            await adaptive.process(turn(conversation_id=f"c{index}",
                                        message_id=f"m{index}"),
                                   now=NOW + timedelta(days=index))
        stats = await adaptive.candidates.stats()
        total = sum(stats[s] for s in C.STATES)
        require(total <= 1, f"aus einem Gedanken wurden {total} Kandidaten")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# Zustandsmaschine
# =====================================================================

async def t_terminal_states_are_terminal() -> None:
    store = CandidateStore(tempfile.mkdtemp())
    try:
        for terminal in C.TERMINAL_STATES:
            require_equal(C.ALLOWED[terminal], frozenset(),
                          f"{terminal} hat einen Ausgang")
    finally:
        await store.close()


async def t_a_forbidden_transition_raises() -> None:
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = await store.observe(
            C.Candidate(id="", statement="x", dedup_key="k", kind="stated",
                        memory_type="preference", subject="s",
                        sensitivity="personal"),
            C.Evidence(at=NOW.isoformat(), kind="stated"), now=NOW)
        # GATHERING -> DECLINED gibt es nicht: ein sammelnder Kandidat ist
        # unsichtbar, also kann ihn niemand ablehnen. Der Weg fuehrt ueber die
        # Frage.
        await store.transition(cand.id, C.ASK_PENDING, reason="sensitive", now=NOW)
        await store.transition(cand.id, C.DECLINED, now=NOW)
        for target in (C.ADOPTED, C.GATHERING, C.ASK_PENDING, C.CONTESTED):
            try:
                await store.transition(cand.id, target, now=NOW)
            except C.IllegalTransition:
                continue
            raise AssertionError(f"declined -> {target} war erlaubt")
    finally:
        await store.close()


async def t_only_one_open_candidate_per_thought() -> None:
    store = CandidateStore(tempfile.mkdtemp())
    try:
        for index in range(4):
            await store.observe(
                C.Candidate(id="", statement="x", dedup_key="same", kind="stated",
                            memory_type="preference", subject="s",
                            sensitivity="personal"),
                C.Evidence(at=NOW.isoformat(), kind="stated",
                           conversation_id=f"c{index}", message_id=f"m{index}"),
                now=NOW)
        open_now = await store.list_states(*C.OPEN_STATES)
        require_equal(len(open_now), 1, f"{len(open_now)} offene Kandidaten")
        require_equal(len(open_now[0].evidence), 4, "Evidenz ging verloren")
    finally:
        await store.close()


async def t_candidates_expire_and_are_not_asked_again() -> None:
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = await store.observe(
            C.Candidate(id="", statement="x", dedup_key="k", kind="stated",
                        memory_type="preference", subject="s",
                        sensitivity="personal"),
            C.Evidence(at=NOW.isoformat(), kind="stated"), now=NOW)
        await store.transition(cand.id, C.ASK_PENDING, reason="sensitive", now=NOW)
        await store.expire_due(now=NOW + timedelta(days=C.ASK_DAYS + 1))
        after = await store.get(cand.id)
        require_equal(after.state, C.EXPIRED, "eine unbeantwortete Frage blieb offen")
        require_equal(len(await store.pending_decisions()), 0,
                      "sie wird weiterhin angezeigt")
    finally:
        await store.close()


async def t_a_clock_jump_backwards_never_adopts() -> None:
    store = CandidateStore(tempfile.mkdtemp())
    try:
        cand = await store.observe(
            C.Candidate(id="", statement="x", dedup_key="k", kind="stated",
                        memory_type="preference", subject="s",
                        sensitivity="personal"),
            C.Evidence(at=NOW.isoformat(), kind="stated"), now=NOW)
        await store.transition(cand.id, C.ASK_PENDING, reason="sensitive", now=NOW)
        await store.expire_due(now=NOW - timedelta(days=400))
        after = await store.get(cand.id)
        require_equal(after.state, C.ASK_PENDING, "ein Uhrensprung veraenderte etwas")
    finally:
        await store.close()


# =====================================================================
# Nutzerentscheide
# =====================================================================

async def t_a_confirmation_is_an_authority_change_not_a_field() -> None:
    adaptive, service, _ = await _build(proposal(
        statement="Hat Diabetes.", memory_type="user",
        subject="user:gesundheit", sensitivity="sensitive"))
    try:
        await adaptive.process(turn("Ich habe seit einem Jahr Diabetes."), now=NOW)
        pending = await adaptive.candidates.pending_decisions()
        memory_id = await adaptive.confirm_candidate(pending[0].id, now=NOW)
        require(memory_id, "die Bestaetigung erzeugte nichts")
        record = await service.semantic.get(memory_id)
        require_equal(record.source_type, SourceType.USER_DIRECT,
                      "eine Bestaetigung erzeugte keinen Nutzer-Record")
        require_equal(record.confidence, 1.0, "Sicherheit nicht gesetzt")
        require_equal(L.lifecycle_of(record), L.CONFIRMED, "falscher Lebenszyklus")
        require_equal(record.sensitivity, Sensitivity.SENSITIVE,
                      "die Schutzstufe ging verloren")
    finally:
        await _teardown(adaptive, service)


async def t_a_decline_holds() -> None:
    adaptive, service, _ = await _build(proposal(
        statement="Hat Diabetes.", memory_type="user",
        subject="user:gesundheit", sensitivity="sensitive"))
    try:
        await adaptive.process(turn("Ich habe seit einem Jahr Diabetes."), now=NOW)
        pending = await adaptive.candidates.pending_decisions()
        require(await adaptive.decline_candidate(pending[0].id, now=NOW),
                "die Ablehnung wirkte nicht")
        result = await adaptive.process(
            turn("Ich habe seit einem Jahr Diabetes.", conversation_id="c9",
                 message_id="m9"), now=NOW)
        require("suppressed" in result.reasons,
                f"nach einer Ablehnung wurde erneut gefragt: {result.reasons}")
        require_equal(len(await adaptive.candidates.pending_decisions()), 0,
                      "der abgelehnte Vorschlag steht wieder da")
    finally:
        await _teardown(adaptive, service)


async def t_a_decision_cannot_be_made_twice() -> None:
    adaptive, service, _ = await _build(proposal(
        statement="Hat Diabetes.", memory_type="user",
        subject="user:gesundheit", sensitivity="sensitive"))
    try:
        await adaptive.process(turn("Ich habe seit einem Jahr Diabetes."), now=NOW)
        pending = await adaptive.candidates.pending_decisions()
        first = await adaptive.confirm_candidate(pending[0].id, now=NOW)
        second = await adaptive.confirm_candidate(pending[0].id, now=NOW)
        require(first, "die erste Entscheidung wirkte nicht")
        require_equal(second, "", "dieselbe Entscheidung wirkte zweimal")
        require_equal(len(await _active(service)), 1, "es entstand ein zweiter Record")
    finally:
        await _teardown(adaptive, service)


# =====================================================================
# Missgestalt und Ausfall
# =====================================================================

async def t_malformed_extractor_output_fails_closed() -> None:
    for payload in ("kein json", "{}", '{"proposals": "nicht-liste"}',
                    '{"proposals": [{"statement": 42}]}',
                    '{"proposals": [{"kind": "erfunden"}]}',
                    '{"proposals": [{"statement": "x", "kind": "stated",'
                    ' "memory_type": "gibt_es_nicht", "subject": "s"}]}',
                    "[]", "null"):
        result = parse(payload)
        require_equal(len(result.proposals), 0,
                      f"Missgestalt ergab Vorschlaege: {payload[:40]!r}")


async def t_the_affect_flag_is_defined_narrowly() -> None:
    """`affect` meint einen Gefuehlszustand, nicht eine warm gesagte Vorliebe.

    Live gemessen: „Ich moechte am liebsten eine klare Empfehlung" wurde vom
    Extraktor mit `affect` markiert und damit verworfen — „am liebsten" ist im
    Deutschen aber die normale Art, eine dauerhafte Vorliebe auszudruecken.
    Das Flag blockiert weiterhin (so will es die Architektur); die Anweisung
    sagt dem Modell jetzt, was es bedeutet.
    """
    from solvio.memory.adaptive import extractor as E

    require("affect" in E.SYSTEM_PROMPT, "das Flag ist nicht mehr erklaert")
    require("Gefuehlszustand" in E.SYSTEM_PROMPT,
            "affect ist nicht als Gefuehlszustand definiert")
    for phrase in ("am liebsten", "ich mag lieber"):
        require(phrase in E.SYSTEM_PROMPT,
                f"die Anweisung nennt {phrase!r} nicht als normale Vorliebe")
    require("affect" in P.BLOCKING_FLAGS,
            "affect blockiert nicht mehr — das waere eine Lockerung")


async def t_an_unknown_flag_cannot_smuggle_anything_in() -> None:
    result = parse({"proposals": [dict(
        statement="Bevorzugt kurze Antworten.", kind="stated",
        memory_type="preference", subject="s", about="self",
        sensitivity="personal", flags=["erfunden", "hypothetical"])]})
    require_equal(len(result.proposals), 1, "der Vorschlag ging verloren")
    require_equal(result.proposals[0].flags, frozenset({"hypothetical"}),
                  "ein erfundenes Flag ueberlebte")


async def t_a_missing_sensitivity_defaults_to_the_stricter_class() -> None:
    """Ein fehlendes Feld darf nie die mildere Behandlung ausloesen."""
    result = parse({"proposals": [dict(
        statement="Bevorzugt kurze Antworten.", kind="stated",
        memory_type="preference", subject="s", about="self", flags=[])]})
    require_equal(result.proposals[0].sensitivity, Sensitivity.SENSITIVE,
                  "ein fehlendes Label wurde als harmlos gelesen")


async def t_a_flood_of_proposals_is_bounded() -> None:
    payload = {"proposals": [dict(
        statement=f"Bevorzugt Variante {i}.", kind="stated",
        memory_type="preference", subject=f"s{i}", about="self",
        sensitivity="personal", flags=[]) for i in range(50)]}
    result = parse(payload)
    require(len(result.proposals) <= 4, f"{len(result.proposals)} Vorschlaege")
    require(result.rejected >= 46, "der Rest wurde still abgeschnitten")


async def t_an_extractor_failure_leaves_everything_else_alone() -> None:
    class Broken:
        name = "broken"

        async def propose(self, turn, *, context=""):
            raise RuntimeError("Anbieter weg")

    adaptive, service, _ = await _build()
    adaptive.extractor = Broken()
    try:
        try:
            await adaptive.process(turn(), now=NOW)
        except RuntimeError:
            raise AssertionError("ein Extraktorfehler kam nach oben durch")
        except Exception:
            pass
        require_equal(len(await _active(service)), 0, "trotz Ausfall geschrieben")
    finally:
        await _teardown(adaptive, service)


async def t_observing_a_turn_never_blocks() -> None:
    """Der Aufrufer im Reader ist synchron. `observe_turn` darf nie warten."""
    import inspect

    from solvio.memory.adaptive.pipeline import AdaptiveMemory as A
    require(not inspect.iscoroutinefunction(A.observe_turn),
            "observe_turn ist asynchron geworden")
    source = inspect.getsource(A.observe_turn)
    require("await" not in source, "observe_turn wartet auf etwas")


# =====================================================================
# Neustart
# =====================================================================

async def t_state_survives_a_restart() -> None:
    """Kandidat, Frage, Unterdrueckung und gelerntes Wissen ueberleben."""
    folder = tempfile.mkdtemp(prefix="solvio-restart-")
    adaptive, service, _ = await _build(
        proposal(statement="Mag Kaffee.", subject="pref:kaffee"), base=folder)
    await adaptive.process(turn("Ich mag Kaffee sehr gerne."), now=NOW)
    store = adaptive.candidates
    gathering = await store.observe(
        C.Candidate(id="", statement="offen", dedup_key="offen", kind="inferred",
                    memory_type="preference", subject="s", sensitivity="personal"),
        C.Evidence(at=NOW.isoformat(), kind="observed", conversation_id="c1"),
        now=NOW)
    asked = await store.observe(
        C.Candidate(id="", statement="gefragt", dedup_key="gefragt", kind="stated",
                    memory_type="user", subject="s2", sensitivity="sensitive"),
        C.Evidence(at=NOW.isoformat(), kind="stated", conversation_id="c1"),
        now=NOW)
    await store.transition(asked.id, C.ASK_PENDING, reason="sensitive", now=NOW)
    await store.suppress("verboten", reason=C.SUPPRESS_DECLINED, now=NOW)
    learned = len(await _active(service))
    await _teardown(adaptive, service)

    again, service2, _ = await _build(proposal(), base=folder)
    try:
        require_equal(len(await _active(service2)), learned,
                      "gelerntes Wissen ueberlebte den Neustart nicht")
        require_equal((await again.candidates.get(gathering.id)).state, C.GATHERING,
                      "ein sammelnder Kandidat ging verloren")
        require_equal((await again.candidates.get(asked.id)).state, C.ASK_PENDING,
                      "eine offene Frage ging verloren")
        require(await again.candidates.is_suppressed("verboten"),
                "eine Unterdrueckung ging verloren")
    finally:
        await _teardown(again, service2)


async def t_a_restart_creates_no_duplicate_adoption() -> None:
    folder = tempfile.mkdtemp(prefix="solvio-dup-")
    adaptive, service, _ = await _build(
        proposal(statement="Mag Kaffee.", subject="pref:kaffee"), base=folder)
    await adaptive.process(turn("Ich mag Kaffee sehr gerne."), now=NOW)
    await _teardown(adaptive, service)

    again, service2, _ = await _build(
        proposal(statement="Mag Kaffee.", subject="pref:kaffee"), base=folder)
    try:
        await again.process(turn("Ich mag Kaffee sehr gerne.",
                                 conversation_id="c2", message_id="m2"), now=NOW)
        require_equal(len(await _active(service2)), 1,
                      "nach dem Neustart entstand ein zweiter Record")
    finally:
        await _teardown(again, service2)


# =====================================================================
# Logging-Hygiene
# =====================================================================

async def t_every_exit_from_process_is_logged() -> None:
    """Ein Pfad, der ueber Gedaechtnis entscheidet, darf nicht unbemerkt enden.

    Bei der ersten Live-Abnahme lief ein Turn sauber durch, es passierte
    nichts, und im Log stand kein Hinweis darauf, wo er geblieben war:
    `process()` hatte vier Ausgaenge, und drei davon schwiegen.
    """
    import ast
    import inspect

    from solvio.memory.adaptive import pipeline as PL
    tree = ast.parse(inspect.getsource(PL))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "AdaptiveMemory")
    process = next(n for n in cls.body if getattr(n, "name", "") == "process")
    returns = [n for n in ast.walk(process) if isinstance(n, ast.Return)]
    require(returns, "process() hat keinen Ausgang")
    for node in returns:
        require(isinstance(node.value, ast.Call)
                and getattr(node.value.func, "attr", "") == "_done",
                f"stiller Ausgang in process(), Zeile {node.lineno}")

    # Und `observe_turn` schweigt in keinem Zweig.
    observe = next(n for n in cls.body if getattr(n, "name", "") == "observe_turn")
    source = ast.get_source_segment(inspect.getsource(PL), observe) or ""
    require(source.count("return False") <= source.count("log.info"),
            "observe_turn hat mehr stille Ausstiege als Protokollzeilen")


async def t_the_pipeline_logs_codes_never_sentences() -> None:
    """Ein Betriebsprotokoll darf nicht mitschreiben, was jemand gesagt hat."""
    import inspect

    from solvio.memory.adaptive import pipeline as PL
    source = inspect.getsource(PL)
    for line in source.split("\n"):
        if "log." not in line:
            continue
        for forbidden in ("turn.text", "proposal.statement", "cand.statement",
                          "record.content", "transcript"):
            require(forbidden not in line,
                    f"eine Log-Zeile traegt Inhalt: {line.strip()[:80]}")


# =====================================================================
# Provider Broker (DEBT-0145) — kein direkter Anbieterzugang
# =====================================================================

class _FakeExtractorTransport:
    """Zeichnet auf, was der Extraktor auf die Rueckschleife schickt."""

    def __init__(self, reply: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.reply = reply if reply is not None else {
            "ok": True, "content": '{"proposals": []}'}

    async def __call__(self, payload: dict, *, token: str = "",
                       port: int = 0, timeout: float = 0.0) -> dict:
        self.calls.append({"payload": payload, "token": token, "port": port})
        return dict(self.reply)


async def t_startup_uses_subscription_without_a_broker_or_implicit_cost_authority() -> None:
    """N4: echte Abo-Naht, aber kein Aufruf ohne gebundene Kostenbeobachtung."""
    from solvio.memory.adaptive import extractor as E

    class Settings:
        openai_api_key = "irrelevant-provider-credential-placeholder"
        adaptive_memory_model = ""

    without = E.from_settings(Settings())
    require(isinstance(without, E.SubscriptionExtractor), "Abo-Anschluss fehlt")
    result = await without.propose(turn())
    require_equal(result.proposals, (), "ungebundener Extraktor lieferte Vorschlaege")
    require_equal(result.reason, "cost_context_missing", "Kostenbindung fehlt")

    from _cognition_fixtures import FakeBroker
    broker = FakeBroker()
    withit = E.from_settings(Settings(), broker=broker)
    require(isinstance(withit, E.SubscriptionExtractor), "Broker wechselte die Route")
    require(not hasattr(withit, "broker"), "Abo-Extraktor bekam einen API-Rueckfall")


async def t_a_fresh_token_and_a_leased_call_per_proposal() -> None:
    """Je Aufruf: neuer Token, eigenes Lease, Schliessen im Erfolgsfall."""
    from _cognition_fixtures import FakeBroker

    from solvio.memory.adaptive import extractor as E
    from solvio.provider_broker.service import ADAPTIVE_EXTRACTOR_PRINCIPAL

    broker = FakeBroker()
    transport = _FakeExtractorTransport()
    ext = E.OpenAIExtractor(broker, transport=transport)

    await ext.propose(turn())
    await ext.propose(turn())

    require_equal(broker.registered,
                  [ADAPTIVE_EXTRACTOR_PRINCIPAL, ADAPTIVE_EXTRACTOR_PRINCIPAL],
                  "kein frischer Token je Aufruf")
    require_equal(len(broker.leases), 2, "kein eigenes Lease je Aufruf")
    require_equal(len(broker.closed), 2, "das Lease wurde nicht geschlossen")
    require_equal(len(transport.calls), 2, "der Aufruf ging nicht ueber den Transport")
    # Der Token, der den Aufruf traegt, ist genau der frisch gepraegte —
    # niemals ein gemerkter aus einer frueheren Runde.
    require_equal(transport.calls[0]["token"],
                  "broker-token-fake-0001",
                  "der Aufruf trug nicht den frisch gepraegten Token")
    require_equal(transport.calls[1]["token"],
                  "broker-token-fake-0002",
                  "der zweite Aufruf trug den alten Token weiter")


async def t_the_call_reaches_the_broker_loopback_never_the_provider_directly() -> None:
    """`kein_direkter_anbieter`: das Ziel ist die Rueckschleife des Brokers.

    Diese Zusicherung prueft VERHALTEN, nicht Textvorkommen: der
    Vorgabetransport des Extraktors ist wortgleich derselbe wie der des
    kognitiven Routers gegen `127.0.0.1`, nie ein zweiter, eigener Weg zum
    Anbieter.
    """
    from solvio.memory.adaptive import extractor as E

    ext = E.OpenAIExtractor(object())
    require(ext._transport is E.broker_transport,
            "der Extraktor hat keinen Vorgabetransport ueber den Broker")

    captured: dict = {}

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return {"choices": [{"message": {"content": '{"proposals": []}'}}]}

    class _FakeSession:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, *, json=None, headers=None):
            captured["url"] = url
            captured["auth"] = (headers or {}).get("Authorization", "")
            return _FakeResponse()

    class _FakeAiohttp:
        ClientSession = _FakeSession

        class ClientTimeout:
            def __init__(self, *a, **k) -> None:
                pass

    real = sys.modules.get("aiohttp")
    sys.modules["aiohttp"] = _FakeAiohttp()
    try:
        result = await E.broker_transport({"model": "gpt-4.1-mini"},
                                          token="tok-abc", port=59999)
    finally:
        if real is not None:
            sys.modules["aiohttp"] = real
        else:
            del sys.modules["aiohttp"]

    require(result.get("ok"), "der gemakelte Aufruf schlug unerwartet fehl")
    require(captured["url"].startswith("http://127.0.0.1:59999/"),
            f"das Ziel ist nicht die Rueckschleife: {captured['url']!r}")
    require("api.openai.com" not in captured["url"],
            "der Aufruf erreichte den Anbieter direkt")
    require(captured["auth"] == "Bearer tok-abc",
            "der Broker-Token wurde nicht als Autorisierung geschickt")


async def t_a_denied_lease_becomes_a_reason_never_an_exception() -> None:
    """Keine Kappe darf die Stimme umwerfen — nur der Vorschlag entfaellt."""
    from _cognition_fixtures import FakeBroker

    from solvio.memory.adaptive import extractor as E
    from solvio.provider_broker.service import ADAPTIVE_EXTRACTOR_PRINCIPAL

    broker = FakeBroker(capped={ADAPTIVE_EXTRACTOR_PRINCIPAL})
    transport = _FakeExtractorTransport()
    ext = E.OpenAIExtractor(broker, transport=transport)

    result = await ext.propose(turn())
    require_equal(result.proposals, (), "trotz Kappe entstanden Vorschlaege")
    require(result.reason, "eine abgewiesene Kappe blieb ohne Grund")
    require_equal(len(transport.calls), 0,
                  "ohne Lease wurde trotzdem ausgeliefert")
    require_equal(len(broker.closed), 0,
                  "ein nie geoeffnetes Lease wurde geschlossen")


async def t_a_broker_denial_becomes_a_reason_never_an_exception() -> None:
    """Der Broker meldet `ok: False` — das darf nie nach oben durchschlagen."""
    from _cognition_fixtures import FakeBroker

    from solvio.memory.adaptive import extractor as E

    broker = FakeBroker()
    transport = _FakeExtractorTransport(
        reply={"ok": False, "reason": "broker_unreachable:ClientError"})
    ext = E.OpenAIExtractor(broker, transport=transport)

    result = await ext.propose(turn())
    require_equal(result.proposals, (), "eine Absage lieferte trotzdem Vorschlaege")
    require_equal(result.reason, "broker_unreachable:ClientError",
                  "der Grund des Brokers ging verloren")
    require_equal(len(broker.closed), 1,
                  "das Lease wurde nach einer Absage nicht geschlossen")


async def t_a_transport_exception_never_reaches_the_caller() -> None:
    """Der Broker ist unerreichbar — `propose` bleibt trotzdem eine Antwort."""
    from _cognition_fixtures import FakeBroker

    from solvio.memory.adaptive import extractor as E

    class ExplodingTransport:
        async def __call__(self, payload, *, token="", port=0, timeout=0.0):
            raise RuntimeError("Netz weg")

    broker = FakeBroker()
    ext = E.OpenAIExtractor(broker, transport=ExplodingTransport())

    try:
        result = await ext.propose(turn())
    except Exception:  # noqa: BLE001
        raise AssertionError("ein Transportfehler kam nach oben durch")
    require_equal(result.proposals, (), "trotz Ausnahme entstanden Vorschlaege")
    require_equal(result.reason, "provider_error", "der Grund fehlt")
    require_equal(len(broker.closed), 1,
                  "das Lease wurde nach einer Ausnahme nicht geschlossen")


async def t_the_extractor_principal_may_ask_for_exactly_one_model() -> None:
    """Der Auftraggeber ist der Zugang, der Modellname keine Berechtigung.

    Eine Kappe ohne Modellgrenze waere die Einladung, aus dem Extraktor heraus
    das teure Modell anzufordern — er extrahiert einen Vorschlag aus einem
    Satz, dafuer gibt es genau ein Modell. Ohne diese Zusicherung faellt die
    Grenze lautlos, sobald jemand die Menge erweitert: sie steht in einer
    Zeile, die aussieht wie eine Konfiguration, und ist eine Sicherheitsgrenze.
    """
    from solvio.provider_broker import session as SESS
    from solvio.provider_broker.service import ADAPTIVE_EXTRACTOR_PRINCIPAL

    kappen = SESS._default_caps(ADAPTIVE_EXTRACTOR_PRINCIPAL)
    require_equal(sorted(kappen.allowed_models), ["gpt-4.1-mini"],
                  f"der Extraktor darf mehr als ein Modell: "
                  f"{sorted(kappen.allowed_models)}")
    # Und die Gegenprobe: es ist WIRKLICH seine eigene Kappe, nicht die
    # Vorgabe, die zufaellig dasselbe saehe.
    vorgabe = SESS.Caps()
    require(kappen is not vorgabe and kappen.max_leases == 1,
            f"das ist nicht die Kappe des Extraktors, sondern eine Vorgabe: "
            f"max_leases={kappen.max_leases}")


async def t_no_provider_credential_field_is_read_by_the_extractor_module() -> None:
    """`kein_direkter_anbieter`, verhaltensbasiert: kein Attribut zieht den
    Anbieterschluessel aus den Einstellungen.

    Statt einer Textsuche nach `api.openai.com` wird das VERHALTEN geprueft:
    ein `settings`-Objekt, dessen `openai_api_key`-Zugriff eine Ausnahme
    wirft, darf `from_settings` nicht stoeren — das Modul liest das Feld gar
    nicht mehr.
    """
    from solvio.memory.adaptive import extractor as E

    class ExplodingSettings:
        adaptive_memory_model = "gpt-4.1-mini"

        @property
        def openai_api_key(self):
            raise AssertionError("der Anbieterschluessel wurde gelesen")

    from _cognition_fixtures import FakeBroker
    without = E.from_settings(ExplodingSettings())
    require(isinstance(without, E.SubscriptionExtractor), "Abo-Anschluss fehlt")
    withit = E.from_settings(ExplodingSettings(), broker=FakeBroker())
    require(isinstance(withit, E.SubscriptionExtractor), "Broker wechselte die Route")
    require(not hasattr(withit, "api_key"),
            "der Extraktor haelt weiterhin ein Anbieterschluessel-Attribut")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
