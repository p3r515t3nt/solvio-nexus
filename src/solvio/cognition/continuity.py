"""Das Arbeitsregister einer Konversation — Verweise, keine Kopien.

Der Router braucht eine Antwort auf zwei Fragen, und beide sind Verweisfragen:
*laeuft die Arbeit, um die gerade gebeten wird, schon?* und *worauf bezieht
sich „und kannst du das gleich beheben"?*

**Konversationsgebunden by construction.** Das Register wird nicht aus den
Buechern der Laufzeit zusammengesucht und dann gefiltert — es wird aus den
Entscheidungen DIESER Konversation aufgebaut, und der Zustand wird je Verweis
nachgeschlagen. Fremde Arbeit kann so gar nicht erst hineingeraten; eine
Kennung, die jemand in einen Inhalt geschrieben hat, findet keinen Eintrag und
faellt damit weg (§3 des Vertrags).

Der Textprozessor kann zusaetzlich gepruefte Verweise anderer Owner-Chats
uebergeben. Sie stehen getrennt in `related_entries`, nie im aktiven Register
oder in den Nutzer-Turns dieses Gespraechs.

**Alles, was ein Executor geschrieben hat, ist Information.** Ergebniszeilen
sind gekappt, redigiert und ausdruecklich als `untrusted_executor` gerahmt. Sie
duerfen den Einschaetzer informieren; autorisieren duerfen sie nichts — und in
ein torgemessenes Argument wandern sie nie.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from solvio.cognition import models as M
from solvio.logging_setup import get_logger

log = get_logger("cognition")

#: Die Rahmung, unter der Executor-Text in die Einschaetzung geht. Dasselbe
#: Wort wie in `deep/events.py` und `bots/answer.py` — zwei Woerter fuer
#: dieselbe Sache waeren zwei Wahrheiten.
CONTENT_TRUST = "untrusted_executor"

#: Zustaende, die „laeuft noch" bedeuten. Der Rest ist erledigt.
_ACTIVE_DEEP: frozenset[str] = frozenset({"queued", "running", "waiting_for_user"})


@dataclass
class WorkEntry:
    """Ein Eintrag des Registers. Eine Zeile, vier Felder, kein Text."""

    work_id: str
    route: str
    state: str
    summary: str = ""
    active: bool = False
    source_conversation_id: str = ""
    source_title: str = ""
    match_kind: str = ""

    def as_line(self) -> str:
        summary = self.summary[:M.REGISTER_SUMMARY_CHARS]
        return f"{self.work_id} | {self.route} | {self.state} | {summary}"


@dataclass
class RelatedContext:
    """Vom Textprozessor gepruefte Verweise anderer Chats, keine Autoritaet.

    Der Aufrufer prueft Owner, persistente Task-Verknuepfung und aktuellen
    Lauf. Historischer Text bleibt auch nach dieser Pruefung unvertraut.
    """

    entries: list[WorkEntry] = field(default_factory=list)
    context: str = ""
    incomplete: bool = False
    selection_pending: bool = False


@dataclass
class ContinuityView:
    """Was der Einschaetzer ueber die bisherige Arbeit sehen darf."""

    conversation_ref: str = ""
    entries: list[WorkEntry] = field(default_factory=list)
    #: Der Turn, zu dem eine Rueckfrage offen ist — leer, wenn keine offen ist.
    pending_clarification_turn: str = ""
    #: Der vorige NUTZER-Turn dieses Gespraechs. Der schwaechere, aber immer
    #: vorhandene Fall: „die einen Zwischenstand haben" ist ohne ihn keine
    #: Aufgabe, sondern ein Satzfragment.
    #:
    #: **Warum ueberhaupt.** Gemessen am Vorfall vom 06.09.2026: SOLVIO fragte
    #: nach („Meinst du die Landtagswahl…?"), der Mensch antwortete, und weil
    #: die Rueckfrage vom SPRACHMODELL kam und nicht von der Route `klaerung`,
    #: stand hier nichts. Die Antwort wurde allein bewertet — ein
    #: Transkriptionsfehler im ersten Wort reichte, und aus „Wahlen" wurden
    #: „Beine einer Person".
    #:
    #: **Ausschliesslich Nutzertext.** Nie eine Assistenten- oder
    #: Executorzeile: dieser Wert weitet ueber `scope` die Ueberlappungspruefung
    #: (`policy.py`), und was dort hineinreicht, koennte ein Ziel verankern.
    #: Fremder Text darf das nicht.
    prior_turn_ref: str = ""
    #: SOLVIOs letzte konkrete Rueckfrage an den Menschen — leer, wenn keine
    #: offen ist.
    #:
    #: **Wozu.** „Meinst du den DAX von gestern?" — „Genau das." ist eine
    #: Klaerung MIT Bestaetigung. Ohne die Frage im Pruefumfang faellt das
    #: verstandene Ziel durch die Ueberlappungsregel, und aus der Bestaetigung
    #: wird eine sinnlose Suchanfrage. Gemessen am Vorfall vom 07.09.2026:
    #: `overlap = 0.00`, ersetzt durch „Geçen? Genau das.".
    #:
    #: **Streng gebunden.** Dieselbe Unterhaltung, die UNMITTELBAR vorangehende
    #: Assistentennachricht, und sie muss eine Frage sein. Eine Antwort — auch
    #: eine, die Quellentext zitiert — endet nicht mit einem Fragezeichen und
    #: kommt damit strukturell nicht in Betracht. Der ganze Verlauf wird NICHT
    #: zum Auftrag: dieser Wert weitet nur den PRUEFUMFANG, nie den Ersatztext.
    pending_question: str = ""
    #: Der Gespraechsausschnitt (freigegebene 3000-Zeichen-Kappe).
    recent_context: str = ""
    #: Nur Kandidaten, die nach der Budgetierung tatsaechlich im Prompt stehen.
    related_entries: list[WorkEntry] = field(default_factory=list)
    related_context: str = ""
    related_incomplete: bool = False
    related_enabled: bool = False
    related_selection_pending: bool = False

    def known_ids(self) -> set[str]:
        """Die Kennungen, die eine Fortsetzung ueberhaupt nennen darf."""
        return {entry.work_id for entry in [*self.entries, *self.related_entries]
                if entry.work_id}

    def active_ids(self) -> set[str]:
        """Die Arbeit, die noch laeuft."""
        return {entry.work_id for entry in self.entries
                if entry.work_id and entry.active}

    def entry(self, work_id: str) -> WorkEntry | None:
        for candidate in [*self.entries, *self.related_entries]:
            if candidate.work_id == work_id:
                return candidate
        return None

    def register_block(self) -> str:
        """Die Zeilen, die in die Einschaetzung gehen — mit ihrer Rahmung."""
        if not self.entries:
            return "Keine laufende oder juengste Arbeit in diesem Gespraech."
        lines = [entry.as_line() for entry in self.entries[:M.REGISTER_MAX]]
        return "\n".join(lines)


def _redact(text: str) -> str:
    """Zweites Netz. Eine Ergebniszeile ist Executor-Text."""
    try:
        from solvio.specialists.launcher import redact
        return redact(str(text or ""))
    except Exception:  # noqa: BLE001 - eine Rahmung darf nie stoeren
        return ""


#: Die Route, unter der ein per Chat-Link bekannter Auftrag im Register steht.
LINKED_TASK_ROUTE = "auftrag_text"


async def build(dispatcher: Any, ledger: Any, *, conversation_ref: str,
                turn_ref: str = "", now: float = 0.0,
                message_sequence: int | None = None) -> ContinuityView:
    """Setzt das Register zusammen. Liest, schreibt nie.

    Fehlt eine der Quellen — keine Agentenlaufzeit angehaengt, kein tiefer
    Executor, kein Gespraechsspeicher —, faellt genau dieser Teil weg. Der
    Router laeuft dann mit weniger Kontext weiter; er faellt nicht aus.

    `message_sequence` (N8/C3) ist die Sequenz der Nachricht, die gerade
    verarbeitet wird: alle Leser des Gespraechsspeichers werden auf sie
    begrenzt (`upto_sequence` inklusiv fuer den vorigen Turn und die offene
    Rueckfrage, `before_sequence` exklusiv fuer den Ausschnitt). Ohne sie —
    der Sprachweg — ist jeder Aufruf bytegleich zu vorher. Das
    Entscheidungsbuch braucht keine Grenze: eine spaetere Nachricht hat vor
    ihrer Verarbeitung keine Zeile.
    """
    del now  # das Register ist zustandsbezogen, nicht zeitbezogen
    view = ContinuityView(conversation_ref=str(conversation_ref or ""))
    if not view.conversation_ref:
        return view

    try:
        decisions = ledger.recent(view.conversation_ref, limit=M.REGISTER_MAX * 2)
    except Exception as exc:  # noqa: BLE001 - ein unlesbares Buch ist kein Absturz
        log.warning("cognition.register_unreadable", kind=type(exc).__name__)
        return view

    # Die juengste offene Rueckfrage — sie ueberlebt genau bis zur naechsten
    # Kommission dieser Konversation.
    for row in decisions:
        if row.get("outcome") == "clarification":
            view.pending_clarification_turn = str(row.get("turn_ref") or "")
            break
        if row.get("outcome") in ("dispatched", "handed_back", "refused", "failed"):
            break

    agent_states = _agent_state_reader(dispatcher, room_runs=_room_run_bindings(dispatcher, view.conversation_ref))
    deep_states = await _deep_states(dispatcher)

    seen: set[str] = set()
    for row in decisions:
        produced = str(row.get("produced_ref") or "")
        if not produced or produced in seen:
            continue
        seen.add(produced)
        route = str(row.get("route_final") or "")
        state, summary, active = _resolve(produced, agent_states, deep_states)
        view.entries.append(WorkEntry(work_id=produced, route=route,
                                      state=state, summary=summary,
                                      active=active))
        if len(view.entries) >= M.REGISTER_MAX:
            break

    # **Die Auftraege DIESES Chats — und nur dieses.** Ein Auftrag, der aus einem
    # Text-Chat gestartet wurde, steht nicht im Entscheidungsbuch des Routers
    # (dort landen nur Kommissionen), wohl aber im Gespraechsspeicher als
    # `conversation_task_links`. Er kommt hier dazu, sofern das Buch ihn nicht
    # schon ueber `produced_ref` kennt. Fail-soft: kein Speicher, kein Link.
    for task_id in _linked_tasks(dispatcher, view.conversation_ref):
        if task_id in seen or len(view.entries) >= M.REGISTER_MAX:
            continue
        seen.add(task_id)
        state, summary, active = _resolve(task_id, agent_states, deep_states)
        view.entries.append(WorkEntry(work_id=task_id, route=LINKED_TASK_ROUTE,
                                      state=state, summary=summary, active=active))

    view.recent_context = _recent_context(dispatcher, view.conversation_ref,
                                          before_sequence=message_sequence)
    view.prior_turn_ref = _prior_turn(dispatcher, view.conversation_ref, turn_ref,
                                      upto_sequence=message_sequence)
    view.pending_question = _pending_question(dispatcher, view.conversation_ref,
                                              turn_ref, decisions,
                                              upto_sequence=message_sequence)
    return view


def _linked_tasks(dispatcher: Any, conversation_ref: str) -> list[str]:
    """Die Auftragskennungen aus `conversation_task_links` — oder nichts."""
    store = getattr(dispatcher, "conversations", None)
    reader = getattr(store, "task_links", None)
    if reader is None:
        return []
    try:
        links = reader(conversation_ref)
    except Exception as exc:  # noqa: BLE001 - fail-soft statt raten
        log.info("cognition.links_unreadable", kind=type(exc).__name__)
        return []
    out: list[str] = []
    for row in links or []:
        task_id = str(row.get("task_id") or "") if isinstance(row, dict) else ""
        if task_id and task_id not in out:
            out.append(task_id)
    return out


def _bounded(kwargs: dict[str, Any], name: str, value: int | None) -> dict[str, Any]:
    """Die Sequenzgrenze nur mitgeben, wenn es eine gibt — der Sprachweg ruft
    den Speicher damit exakt wie vorher auf."""
    if value is not None:
        kwargs[name] = int(value)
    return kwargs


def _resolve(produced: str, agent_states: Any,
             deep_states: dict[str, str]) -> tuple[str, str, bool]:
    """Der heutige Zustand eines Verweises — aus dem Buch, dem er gehoert."""
    if produced.startswith("at-") and agent_states is not None:
        return agent_states(produced)
    status = deep_states.get(produced, "")
    if status:
        return status, "", status in _ACTIVE_DEEP
    return "unbekannt", "", False


def _room_run_bindings(dispatcher: Any, conversation_ref: str) -> dict[str, str] | None:
    """Room readers keep the exact linked runs, never a later private revision."""
    store = getattr(dispatcher, "conversations", None)
    reader = getattr(store, "conversation", None)
    if reader is None:
        return None
    try:
        row = reader(conversation_ref)
        if not row:
            return {}
        if not row.get("room_device_id"):
            return None
        # Existing link order is chronological. A later *room* continuation may
        # bind a later run; a private follow-up elsewhere never changes this map.
        return {link["task_id"]: link["run_id"] for link in store.task_links(conversation_ref)
                if str(link.get("source", "")).startswith("voice:")}
    except Exception:
        return {}  # An unreadable room binding never widens to latest task data.


def _agent_state_reader(dispatcher: Any, *, room_runs: dict[str, str] | None = None):
    """Ein Leser fuer Aufgabenkennungen — oder `None`, wenn keine Laufzeit da ist.

    Die Laufzeit wird ueber ein Attribut des Dispatchers erreicht und
    ausdruecklich NICHT importiert: ein Modulimport von `solvio.agent_runtime`
    im Kern waere genau die Zusage, die `SOLVIO_AGENT_RUNTIME=off` bricht.
    """
    orchestrator = getattr(dispatcher, "agent_runtime", None)
    book = getattr(orchestrator, "ledger", None)
    if book is None:
        return None

    def read(task_id: str) -> tuple[str, str, bool]:
        try:
            if room_runs is not None:
                run_id = room_runs.get(task_id)
                run = book.get_run(run_id) if run_id else None
                runs = [run] if run is not None and run.task_id == task_id else []
            else:
                runs = book.runs_for_task(task_id)
        except Exception as exc:  # noqa: BLE001 - fail-soft statt raten
            log.info("cognition.task_unreadable", kind=type(exc).__name__)
            return "unbekannt", "", False
        if not runs:
            return "unbekannt", "", False
        run = runs[-1]
        state = str(getattr(run, "state", "") or "")
        summary = _redact(getattr(run, "result_summary", "") or "")
        finished = getattr(run, "finished_at", None)
        return state, summary, finished is None

    return read


async def _deep_states(dispatcher: Any) -> dict[str, str]:
    """Die Zustaende der tiefen Aufgaben. Leer, wenn kein Executor da ist."""
    runtime = getattr(dispatcher, "deep_runtime", None)
    if runtime is None:
        return {}
    try:
        handles = await runtime.list_tasks()
    except Exception as exc:  # noqa: BLE001 - eine Messung darf nie stoeren
        log.info("cognition.deep_unreadable", kind=type(exc).__name__)
        return {}
    states: dict[str, str] = {}
    for handle in handles or []:
        status = getattr(handle, "status", None)
        states[str(getattr(handle, "id", ""))] = str(
            getattr(status, "value", status) or "")
    return states


def _recent_context(dispatcher: Any, conversation_ref: str, *,
                    before_sequence: int | None = None) -> str:
    """Der freigegebene Gespraechsausschnitt — dieselbe Kappe wie sonst."""
    store = getattr(dispatcher, "conversations", None)
    if store is None:
        return ""
    try:
        messages = store.recent_context(
            conversation_ref, **_bounded({"max_chars": M.RECENT_CONTEXT_CHARS},
                                         "before_sequence", before_sequence))
    except Exception as exc:  # noqa: BLE001 - ohne Ausschnitt laeuft es weiter
        log.info("cognition.context_unreadable", kind=type(exc).__name__)
        return ""
    lines = [f"{row.get('role', '')}: {row.get('text', '')}"
             for row in messages or []]
    return "\n".join(lines)[:M.RECENT_CONTEXT_CHARS]


def turn_text(dispatcher: Any, conversation_ref: str, turn_ref: str) -> str:
    """Der GANZE Text eines frueheren Turns — per Verweis, aus seinem Haus.

    Der Gespraechsspeicher bleibt die kanonische Heimat der Sprache des
    Nutzers. Der Router haelt eine Kennung und holt den Text beim Lesen; er
    legt keine zweite Kopie an.

    **Alle Nutzernachrichten des Turns, in Reihenfolge — nicht die letzte.**
    Vorher stand hier `for row in reversed(messages)` mit `return` beim ersten
    Treffer, also die zuletzt persistierte Nachricht. Ein Turn hat aber nicht
    immer nur eine: bei einem Barge-in landet ein finalisiertes Transkript
    unter der Kennung des bereits begonnenen naechsten Turns. Gemessen am
    06.09.2026 um 22:02 trug Turn `t3` zwei Nachrichten —

        „Sind die Wahlen heute?"  und  „Ausgegangen, oder wie stehen die?"

    — und dieser Weg gab nur die zweite zurueck. Die eigentliche Frage fiel aus
    dem BINDENDEN Feld der naechsten Einschaetzung, obwohl sie im
    Gespraechsausschnitt noch stand. Sie war da und zaehlte nicht.
    """
    store = getattr(dispatcher, "conversations", None)
    if store is None or not turn_ref:
        return ""
    try:
        messages = store.messages(conversation_ref, limit=M.TURN_LOOKBACK)
    except Exception as exc:  # noqa: BLE001 - fail-soft
        log.info("cognition.turn_unreadable", kind=type(exc).__name__)
        return ""
    teile = [str(row.get("text") or "").strip() for row in messages or []
             if row.get("source_turn_id") == turn_ref and row.get("role") == "user"]
    return " ".join(teil for teil in teile if teil)


def _prior_turn(dispatcher: Any, conversation_ref: str, turn_ref: str, *,
                upto_sequence: int | None = None) -> str:
    """Der letzte NUTZER-Turn vor dem laufenden — oder nichts.

    Gelesen wird aus dem Gespraechsspeicher, nicht aus dem Entscheidungsbuch.
    Das ist der Unterschied, an dem der Vorfall haengt: das Buch kennt nur
    Rueckfragen, die die ROUTE `klaerung` gestellt hat. Eine Rueckfrage, die
    das Sprachmodell nach einer erledigten Route stellt, steht dort nicht —
    der Mensch hat sie trotzdem gehoert und beantwortet.

    Der laufende Turn selbst faellt weg: seine eigene Aeusserung ist schon der
    Gegenstand der Einschaetzung, und ein zweites Mal derselbe Text waere kein
    Zusammenhang.
    """
    store = getattr(dispatcher, "conversations", None)
    if store is None:
        return ""
    try:
        messages = store.messages(
            conversation_ref, **_bounded({"limit": M.TURN_LOOKBACK},
                                         "upto_sequence", upto_sequence))
    except Exception as exc:  # noqa: BLE001 - fail-soft
        log.info("cognition.turn_unreadable", kind=type(exc).__name__)
        return ""
    laufend = str(turn_ref or "")
    for row in reversed(messages or []):
        if row.get("role") != "user":
            continue
        kennung = str(row.get("source_turn_id") or "")
        if kennung and kennung != laufend:
            return kennung
    return ""


#: Wie lang SOLVIOs Rueckfrage hoechstens in den Pruefumfang eingeht. Sie soll
#: die Bestaetigung tragen, nicht den Umfang fluten.
MAX_QUESTION_CHARS = 300


#: Ausgaenge, bei denen in diesem Turn eine Faehigkeit Inhalt ERZEUGT hat.
#: Eine Assistentennachricht aus einem solchen Turn kann Quellentext tragen.
_PRODUCED = frozenset({"dispatched"})


def _pending_question(dispatcher: Any, conversation_ref: str,
                      turn_ref: str, decisions: Any = (), *,
                      upto_sequence: int | None = None) -> str:
    """SOLVIOs letzte konkrete Rueckfrage — oder nichts.

    **Genau eine Nachricht, dreifach gebunden.** Dieselbe Unterhaltung, die
    unmittelbar vor dem laufenden Turn stehende Assistentennachricht, und ihr
    Turn darf nichts ERZEUGT haben.

    **Der Sicherheitsnachweis ist die Herkunft, nicht das Satzzeichen.** Ein
    Fragezeichen beweist keine vertrauenswuerdige Klaerung: eine Antwort kann
    Quellentext enthalten und mit einer Frage enden — dann waere fremder
    Inhalt ueber diesen Weg in die Zielpruefung geraten.

    Geprueft wird deshalb das Entscheidungsbuch DIESER Konversation: hat der
    Router in jenem Turn etwas beauftragt (`outcome='dispatched'`), stammt der
    Text moeglicherweise aus einer Faehigkeit, und die Nachricht scheidet aus.
    Gemessen am Vorfall vom 07.09.2026 trennt genau das die beiden Faelle: die
    DAX-Antwort steht mit `outcome='dispatched'` im Buch, die Rueckfrage
    „Meinst du den DAX von gestern?" hat dort ueberhaupt keine Zeile — sie kam
    vom Sprachmodell, ohne dass eine Faehigkeit lief.

    Das Fragezeichen bleibt als RELEVANZ-Filter: eine Aussage ist keine
    Rueckfrage. Es traegt aber nicht die Sicherheit.
    """
    store = getattr(dispatcher, "conversations", None)
    if store is None:
        return ""
    erzeugt = {str(row.get("turn_ref") or "") for row in (decisions or [])
               if row.get("outcome") in _PRODUCED}
    try:
        messages = store.messages(
            conversation_ref, **_bounded({"limit": M.TURN_LOOKBACK},
                                         "upto_sequence", upto_sequence))
    except Exception as exc:  # noqa: BLE001 - fail-soft
        log.info("cognition.turn_unreadable", kind=type(exc).__name__)
        return ""
    laufend = str(turn_ref or "")
    for row in reversed(messages or []):
        kennung = str(row.get("source_turn_id") or "")
        if kennung == laufend:
            continue                       # der laufende Turn zaehlt nicht mit
        rolle = row.get("role")
        if rolle == "user":
            # Vor der Rueckfrage steht wieder der Mensch: es gibt keine
            # unmittelbar vorangehende Frage mehr.
            return ""
        if rolle != "assistant":
            continue
        if kennung in erzeugt:
            # In diesem Turn hat eine Faehigkeit gearbeitet. Was SOLVIO danach
            # sagte, kann Quellentext tragen — und Quellentext wird hier nie
            # zum bestaetigten Ziel, auch wenn er mit einer Frage endet.
            log.info("cognition.question_from_produced_turn", turn=kennung[:40])
            return ""
        text = str(row.get("text") or "").strip()
        if text.endswith("?"):
            return text[:MAX_QUESTION_CHARS]
        return ""                          # die juengste war eine Aussage
    return ""


def stamp() -> float:
    """Eine Uhr, die ein Test ersetzen kann."""
    return time.time()
