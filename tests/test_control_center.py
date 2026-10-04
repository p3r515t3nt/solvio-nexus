"""Zeigen, was ist — ohne eine zweite Wahrheit zu erfinden.

Ein Kontrollzentrum ist gefaehrlicher, als es aussieht, und zwar aus zwei
Richtungen.

**Von innen:** die bequeme Bauart waere ein eigener Zwischenspeicher, der die
Zahlen fuer den Bildschirm vorhaelt. Er weicht irgendwann ab — meistens genau
dann, wenn jemand hinsieht, weil etwas nicht stimmt. Deshalb liest hier alles im
Moment der Anfrage aus dem Speicher, aus dem die Zahl ohnehin stammt.

**Von aussen:** ein Bildschirm, der den Zustand des ganzen Systems zeigt, ist ein
schoenes Ziel. Er darf deshalb nichts verraten, was beim Einbruch hilft — keine
Token, keine Pfade, keine Schluessel — und er darf keine Abkuerzung an der
Freigabe vorbei anbieten. Ein Tipp des Besitzers auf seinem angemeldeten Geraet
darf SOLVIOs eigene Buchhaltung aendern; er darf nichts freigeben.

Die dritte Gefahr ist leiser: eine Anzeige, die nicht mehr stimmt, aber so
aussieht. Ein alter Stand muss als alt erkennbar sein, und „ich habe nicht
nachgesehen" darf nicht wie „alles gut" aussehen.
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.control_center import activity as A  # noqa: E402
from solvio.control_center import routes as R  # noqa: E402
from solvio.control_center.health import (  # noqa: E402
    NEEDS_USER, NOT_WELL, Component, HealthBoard, Probe, State,
)
from solvio.control_center.probes import classify_error  # noqa: E402
from solvio.control_center.snapshot import (  # noqa: E402
    ControlCenter, _friendly_error, describe_schedule,
)
from solvio.proactive import store as S  # noqa: E402
from solvio.proactive.schedule import daily, every, in_seconds, weekly  # noqa: E402

CONTROL_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                           "control_center")


def _run(coro):
    return asyncio.run(coro)


def _store() -> S.ProactiveStore:
    return S.ProactiveStore(os.path.join(tempfile.mkdtemp(), "p.sqlite3"))


def _code_only(name: str) -> str:
    """Quelltext ohne Kommentare und Zeichenketten.

    Diese Dateien ERKLAEREN, warum sie keine Geheimnisse ausgeben. Eine
    Wortsuche ueber den Rohtext schlaegt sonst bei genau der Datei an, die es
    richtig macht.
    """
    import io
    import tokenize
    # Ab Python 3.12 zerlegt `tokenize` f-Strings in FSTRING_START/MIDDLE/END.
    # Ein Filter, der nur STRING kennt, laesst ihren Inhalt durch — und dann
    # schlaegt eine Wortsuche bei einem Text an, der gar kein Code ist. Genau
    # daran ist der erste Entwurf dieser Datei gescheitert.
    skip = {tokenize.COMMENT, tokenize.STRING}
    for name_ in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        value = getattr(tokenize, name_, None)
        if value is not None:
            skip.add(value)
    with open(os.path.join(CONTROL_DIR, name), encoding="utf-8") as handle:
        source = handle.read()
    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in skip:
            continue
        kept.append(token.string)
    return " ".join(kept)


class _Approvals:
    def __init__(self, pending=()):
        self._pending = list(pending)

    async def pending(self):
        return list(self._pending)


class _Runtime:
    def __init__(self, pending=(), port=8770):
        self.approvals = _Approvals(pending)
        self.port = port


class _Dispatcher:
    def __init__(self, store=None, *, runtime=None, scheduler=None):
        self.proactive_store = store
        self.approver_runtime = runtime
        self.scheduler = scheduler
        self.capabilities = None
        self.deep_runtime = None


def _board(*states: tuple[str, State, str]) -> HealthBoard:
    """Ein Brett mit fest vorgegebenen Antworten.

    Bewusst OHNE Aktualisierung: `_center()` wird auch innerhalb eines laufenden
    Eventloops gebaut, und `asyncio.run()` darin ist ein Laufzeitfehler. Wer die
    Zustaende braucht, aktualisiert selbst — die Uebersicht tut das ohnehin.
    """
    probes = []
    for key, state, reason in states:
        async def check(state=state, reason=reason):
            return state, reason
        probes.append(Probe(key, key.capitalize(), check, ttl=999.0))
    return HealthBoard(probes)


def _refreshed(*states: tuple[str, State, str]) -> HealthBoard:
    board = _board(*states)
    _run(board.refresh())
    return board


def _center(store=None, board=None, **kwargs) -> ControlCenter:
    return ControlCenter(_Dispatcher(store, **kwargs),
                         board or _board(("core", State.HEALTHY, "laeuft")))


def _task(store, **overrides) -> S.Task:
    now = overrides.pop("now", time.time())
    task = S.Task(
        task_id=overrides.pop("task_id", "bt-1"), owner="gregor",
        title=overrides.pop("title", "Kalender im Blick"), created_at=now,
        created_from=overrides.pop("created_from", "Behalt das im Blick."),
        schedule=overrides.pop("schedule", every(900).as_dict()),
        action=overrides.pop("action", {"kind": "capability",
                                        "capability": "calendar_list_events",
                                        "arguments": {}, "notify": "bei_aenderung"}),
        next_run_at=overrides.pop("next_run_at", now + 900))
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


# -- Keine zweite Wahrheit ---------------------------------------------------

def t_the_control_center_stores_nothing_of_its_own():
    """Jede Zahl kommt im Moment der Anfrage aus dem Speicher, aus dem sie stammt.

    Ein eigener Zwischenspeicher waere eine zweite Wahrheit, und zwei Wahrheiten
    weichen voneinander ab — meistens genau dann, wenn jemand hinsieht.
    """
    fields = set(getattr(ControlCenter, "__annotations__", {}))
    for forbidden in ("cache", "tasks", "items", "events", "snapshot"):
        require(forbidden not in fields, f"eigenes Feld {forbidden}")
    code = _code_only("snapshot.py")
    for forbidden in ("sqlite3", "CREATE TABLE", "INSERT INTO", "open("):
        require(forbidden not in code,
                f"{forbidden}: das Kontrollzentrum haelt eigenen Bestand")
    # Und der Gegenbeweis: es liest wirklich beim Fragen.
    source = inspect.getsource(ControlCenter.overview)
    require("await" in source, "die Uebersicht wird geholt, nicht vorgehalten")


def t_every_number_comes_from_the_live_store():
    async def scenario():
        store = _store()
        center = _center(store)
        before = await center.overview()
        await store.put_task(_task(store, task_id="bt-neu"))
        after = await center.overview()
        return before, after
    before, after = _run(scenario())
    require_equal(before["aufgaben_gesamt"], 0, "vorher keine")
    require_equal(after["aufgaben_gesamt"], 1, "danach eine — ohne Neuaufbau")


# -- Gesundheit ---------------------------------------------------------------

def t_unknown_is_not_green():
    """Ein gruener Punkt, der nur „nicht nachgesehen" bedeutet, ist schlimmer
    als ein grauer."""
    board = _refreshed(("a", State.UNKNOWN, ""), ("b", State.UNKNOWN, ""))
    summary = board.summary()
    require_equal(summary["zustand"], State.UNKNOWN.value, "unbekannt bleibt unbekannt")
    require("nicht nachgesehen" in summary["satz"], "und wird auch so gesagt")
    require(State.UNKNOWN not in NOT_WELL, "unbekannt ist keine Stoerung")
    require(State.UNKNOWN not in NEEDS_USER, "und verlangt nichts vom Nutzer")


def t_an_expired_login_asks_the_user_an_outage_does_not():
    """Der Unterschied traegt: nur eines davon kann ein Mensch beheben."""
    require(State.AUTH_REQUIRED in NEEDS_USER, "Anmeldung braucht den Nutzer")
    require(State.UNAVAILABLE not in NEEDS_USER, "eine Stoerung nicht")
    require(State.QUOTA_LIMITED not in NEEDS_USER, "ein Kontingent auch nicht")
    board = _refreshed(("calendar", State.AUTH_REQUIRED, "Zugang abgelaufen"),
                   ("core", State.HEALTHY, "laeuft"))
    summary = board.summary()
    require_equal(summary["zustand"], State.AUTH_REQUIRED.value,
                  "das schlaegt auf die Gesamtlage durch")
    require_equal(summary["braucht_dich"], ["calendar"], "und benennt, wen es trifft")


def t_google_error_words_become_the_right_state():
    for text, expected in (
            ("invalid_grant: Token has been expired or revoked",
             State.AUTH_REQUIRED),
            ("401 Unauthorized", State.AUTH_REQUIRED),
            ("credentials_missing", State.AUTH_REQUIRED),
            ("Rate limit exceeded, try again", State.QUOTA_LIMITED),
            ("429 Too Many Requests", State.QUOTA_LIMITED),
            ("Connection reset by peer", State.UNAVAILABLE)):
        state, reason = classify_error(text)
        require_equal(state, expected, f"{text!r} -> {expected.value}")
        require(reason, "und immer mit einem Grund")


def t_the_headline_does_not_promise_an_action_that_has_no_section():
    """Eine Stoerung verlangt keine Handlung — also darf die Ueberschrift auch
    keine ankuendigen, waehrend die Rubrik „Braucht dich" leer bleibt."""
    board = _refreshed(("gateway", State.UNAVAILABLE, "nicht erreichbar"),
                       ("hermes", State.UNAVAILABLE, "laeuft nicht"),
                       ("core", State.HEALTHY, "laeuft"))
    summary = board.summary()
    require_equal(summary["braucht_dich"], [], "eine Stoerung fordert nichts")
    require("brauchen einen Blick" not in summary["satz"],
            f"und die Ueberschrift fordert auch nichts: {summary['satz']!r}")
    require("nicht in Ordnung" in summary["satz"], "sie beschreibt nur")

    # Wo wirklich jemand gefragt ist, steht es dagegen sehr wohl.
    auth = _refreshed(("calendar", State.AUTH_REQUIRED, "Zugang abgelaufen"))
    require_equal(auth.summary()["braucht_dich"], ["calendar"], "hier schon")
    require("abgelaufen" in auth.summary()["satz"], "mit dem Grund im Satz")


def t_one_broken_component_does_not_break_the_whole_view():
    board = _refreshed(("core", State.HEALTHY, "laeuft"),
                       ("calendar", State.AUTH_REQUIRED, "abgelaufen"),
                   ("ha", State.HEALTHY, "erreichbar"),
                   ("hermes", State.HEALTHY, "bereit"))
    healthy = [c.key for c in board.known() if c.state is State.HEALTHY]
    require_equal(sorted(healthy), ["core", "ha", "hermes"],
                  "die uebrigen bleiben gruen")
    require_equal(board.summary()["auffaellig"], ["calendar"], "nur das eine faellt auf")


def t_a_probe_that_hangs_becomes_unavailable_not_a_hang():
    async def scenario():
        async def forever():
            await asyncio.sleep(60)
            return State.HEALTHY, ""
        board = HealthBoard([Probe("lahm", "Lahm", forever, ttl=999.0)])
        import solvio.control_center.health as H
        saved = H.PROBE_TIMEOUT
        H.PROBE_TIMEOUT = 0.2
        try:
            started = time.monotonic()
            await board.refresh()
            return board.component("lahm"), time.monotonic() - started
        finally:
            H.PROBE_TIMEOUT = saved
    component, elapsed = _run(scenario())
    require_equal(component.state, State.UNAVAILABLE, "eine haengende Pruefung endet")
    require(elapsed < 5, f"und zwar schnell ({elapsed:.1f}s)")


def t_a_probe_that_raises_does_not_kill_the_board():
    async def scenario():
        async def boom():
            raise RuntimeError("kaputt")

        async def fine():
            return State.HEALTHY, "laeuft"
        board = HealthBoard([Probe("kaputt", "Kaputt", boom, ttl=999.0),
                             Probe("heil", "Heil", fine, ttl=999.0)])
        await board.refresh()
        return board
    board = _run(scenario())
    require_equal(board.component("kaputt").state, State.UNKNOWN,
                  "die kaputte Pruefung meldet Unwissen")
    require_equal(board.component("heil").state, State.HEALTHY,
                  "die andere laeuft trotzdem")


def t_results_are_cached_so_providers_are_not_hammered():
    """Bei fuenf Sekunden Bildschirmtakt waere eine Netzpruefung je Aktualisierung
    ein Dauerfeuer auf Google."""
    calls = {"n": 0}

    async def scenario():
        async def counting():
            calls["n"] += 1
            return State.HEALTHY, "ok"
        now = [1000.0]
        board = HealthBoard([Probe("teuer", "Teuer", counting, ttl=300.0)],
                            clock=lambda: now[0])
        await board.refresh()
        await board.refresh()
        await board.refresh()
        first = calls["n"]
        now[0] += 301
        await board.refresh()
        return first, calls["n"]
    first, total = _run(scenario())
    require_equal(first, 1, "drei Aktualisierungen, eine Pruefung")
    require_equal(total, 2, "erst nach Ablauf wird wieder gefragt")


def t_last_success_is_only_stamped_on_success():
    async def scenario():
        state = {"value": State.HEALTHY}

        async def flipping():
            return state["value"], "so ist es"
        now = [1000.0]
        board = HealthBoard([Probe("x", "X", flipping, ttl=0.0)],
                            clock=lambda: now[0])
        await board.refresh()
        good = board.component("x").last_success_at
        state["value"] = State.UNAVAILABLE
        now[0] += 100
        await board.refresh()
        return good, board.component("x")
    good, component = _run(scenario())
    require_equal(component.last_success_at, good,
                  "der letzte gute Zeitpunkt bleibt stehen")
    require(component.last_checked_at > component.last_success_at,
            "geprueft wurde spaeter als es zuletzt gut war")


# -- Uebersicht ---------------------------------------------------------------

def t_the_overview_counts_what_it_says_it_counts():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        await store.put_task(_task(store, task_id="bt-b", enabled=False,
                                   state=S.PAUSED))
        await store.add_item({"notification_id": "pn-1", "task_id": "bt-a",
                              "run_id": "r", "priority": "normal",
                              "summary": "etwas", "fingerprint": "f",
                              "content_trust": ""})
        center = _center(store, runtime=_Runtime(pending=[{"approval_id": "ap-1"}]))
        return await center.overview()
    overview = _run(scenario())
    require_equal(overview["aufgaben_gesamt"], 2, "beide Aufgaben")
    require_equal(overview["aufgaben_aktiv"], 1, "aber nur eine aktiv")
    require_equal(overview["ungelesen"], 1, "eine ungelesene Meldung")
    require_equal(overview["freigaben_offen"], 1, "eine wartende Freigabe")
    require_equal(overview["freigabe_id"], "ap-1", "und ihre Kennung zum Hinspringen")


def t_a_waiting_approval_is_the_first_thing_the_overview_says():
    async def scenario():
        center = _center(_store(),
                         runtime=_Runtime(pending=[{"approval_id": "ap-9"}]))
        return await center.overview()
    overview = _run(scenario())
    require(overview["aufmerksamkeit"], "es steht etwas unter 'braucht dich'")
    require("Freigabe" in overview["aufmerksamkeit"][0],
            f"und zwar die Freigabe zuerst: {overview['aufmerksamkeit']}")


def t_the_overview_carries_its_own_timestamp():
    """Ohne den koennte das iPhone einen alten Stand als aktuellen zeigen."""
    async def scenario():
        return await _center(_store()).overview()
    overview = _run(scenario())
    require(overview.get("stand"), "ein Zeitstempel ist dabei")
    require(abs(overview["stand"] - time.time()) < 5, "und er ist frisch")


# -- Aufgaben -----------------------------------------------------------------

def t_a_schedule_is_shown_as_a_sentence_not_as_seconds():
    require_equal(describe_schedule(daily(7, 30).as_dict()), "täglich um 07:30")
    require_equal(describe_schedule(weekly((0,), 8, 0).as_dict()),
                  "montags um 08:00")
    require_equal(describe_schedule(every(900).as_dict()), "alle 15 Minuten")
    require_equal(describe_schedule(every(7200).as_dict()), "alle 2 Stunden")
    require_equal(describe_schedule(in_seconds(60, now=time.time()).as_dict()),
                  "einmalig")
    require_equal(describe_schedule({"art": "quatsch"}), "Zeitplan unklar",
                  "und Unverstaendliches wird nicht erfunden")


def t_an_internal_error_becomes_a_sentence_a_person_understands():
    require_equal(_friendly_error(
        "capability_failed: credential_or_connection_missing:invalid_grant"),
        "Zugang abgelaufen — bitte neu anmelden")
    require_equal(_friendly_error("executor_unavailable: device_or_service_unavailable"),
                  "war gerade nicht erreichbar")
    require_equal(_friendly_error(""), "", "und nichts bleibt nichts")


def t_pause_and_resume_touch_exactly_one_task():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a", title="A"))
        await store.put_task(_task(store, task_id="bt-b", title="B"))
        center = _center(store)
        paused = await center.pause("bt-a")
        after_a = await store.get_task("bt-a")
        after_b = await store.get_task("bt-b")
        resumed = await center.resume("bt-a")
        again_a = await store.get_task("bt-a")
        return paused, after_a, after_b, resumed, again_a
    paused, a, b, resumed, again = _run(scenario())
    require(paused["ok"], "das Pausieren ging")
    require(not a.enabled, "A ist pausiert")
    require(b.enabled, "B ist unberuehrt")
    require(resumed["ok"] and again.enabled, "und laesst sich fortsetzen")
    require(again.next_run_at is not None, "mit neuem Termin")


def t_run_now_makes_exactly_that_task_due():
    async def scenario():
        store = _store()
        later = time.time() + 9999
        await store.put_task(_task(store, task_id="bt-a", next_run_at=later))
        await store.put_task(_task(store, task_id="bt-b", next_run_at=later))
        center = _center(store)
        await center.run_now("bt-a")
        due = await store.due_tasks(time.time())
        return [t.task_id for t in due]
    due = _run(scenario())
    require_equal(due, ["bt-a"], f"nur A ist faellig, nicht {due}")


def t_delete_removes_exactly_that_task_and_says_which():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a", title="A"))
        await store.put_task(_task(store, task_id="bt-b", title="B"))
        center = _center(store)
        result = await center.delete("bt-a")
        rest = [t.task_id for t in await store.list_tasks()]
        return result, rest
    result, rest = _run(scenario())
    require(result["ok"], "geloescht")
    require_equal(result["titel"], "A", "und die Antwort nennt WELCHE")
    require_equal(rest, ["bt-b"], "die andere bleibt")


def t_an_action_on_an_unknown_id_changes_nothing():
    """Der Nachweis gegen ein Umlenken: eine veraltete Kennung trifft nichts."""
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        center = _center(store)
        results = [await center.pause("bt-weg"), await center.resume("bt-weg"),
                   await center.run_now("bt-weg"), await center.delete("bt-weg")]
        return results, await store.get_task("bt-a")
    results, survivor = _run(scenario())
    for result in results:
        require(not result["ok"], "eine unbekannte Kennung wird abgelehnt")
        require_equal(result["grund"], "unbekannte_aufgabe", "und zwar benannt")
    require(survivor.enabled, "die vorhandene Aufgabe bleibt unberuehrt")


def t_a_refresh_between_tap_and_action_cannot_retarget_it():
    """Die Kennung wird beim Tippen kopiert, nicht danach nachgeschlagen.

    Dieselbe Lehre wie beim Freigabeweg, wo ein Face ID einmal auf der falschen
    von zwei Anfragen landete. Hier wird sie an der Server-Seite festgehalten:
    die Handlung nimmt eine Kennung, keinen Platz in einer Liste.
    """
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-alt", title="Alt"))
        center = _center(store)
        # Zwischen „Tippen" und „Ausfuehren" aendert sich die Liste vollstaendig.
        await store.put_task(_task(store, task_id="bt-neu1", title="Neu 1"))
        await store.put_task(_task(store, task_id="bt-neu2", title="Neu 2"))
        result = await center.pause("bt-alt")
        return result, [(t.task_id, t.enabled) for t in await store.list_tasks()]
    result, states = _run(scenario())
    require(result["ok"], "die Handlung ging durch")
    paused = [key for key, enabled in states if not enabled]
    require_equal(paused, ["bt-alt"], f"genau die angetippte wurde pausiert: {states}")

    signature = inspect.signature(ControlCenter.pause)
    require("task_id" in signature.parameters, "die Handlung nimmt eine Kennung")
    for forbidden in ("index", "position", "row", "offset"):
        require(forbidden not in signature.parameters, f"und keinen {forbidden}")


def t_run_now_refuses_on_a_paused_task():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-p", enabled=False,
                                   state=S.PAUSED))
        return await _center(store).run_now("bt-p")
    result = _run(scenario())
    require(not result["ok"], "eine pausierte Aufgabe laeuft nicht auf Zuruf")
    require_equal(result["grund"], "pausiert", "und sagt warum")


# -- Posteingang ---------------------------------------------------------------

def t_marking_one_item_read_leaves_the_others_alone():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        for index in range(3):
            await store.add_item({"notification_id": f"pn-{index}",
                                  "task_id": "bt-a", "run_id": "r",
                                  "priority": "normal", "summary": f"Nr {index}",
                                  "fingerprint": f"f{index}", "content_trust": ""})
        center = _center(store)
        result = await center.mark_read("pn-1")
        rest = await store.unread(10)
        return result, sorted(i["id"] for i in rest)
    result, rest = _run(scenario())
    require(result["ok"] and result["geaendert"], "genau eine wurde gelesen")
    require_equal(rest, ["pn-0", "pn-2"], f"die anderen bleiben ungelesen: {rest}")
    require_equal(result["ungelesen"], 2, "und die Zahl stimmt sofort")


def t_marking_the_same_item_twice_is_honest_about_it():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        await store.add_item({"notification_id": "pn-x", "task_id": "bt-a",
                              "run_id": "r", "priority": "normal",
                              "summary": "x", "fingerprint": "f",
                              "content_trust": ""})
        center = _center(store)
        return await center.mark_read("pn-x"), await center.mark_read("pn-x")
    first, second = _run(scenario())
    require(first["geaendert"], "beim ersten Mal aendert sich etwas")
    require(not second["geaendert"], "beim zweiten Mal nicht — und es wird gesagt")
    require(second["ok"], "ein zweiter Tipp ist trotzdem kein Fehler")


def t_the_list_view_carries_no_payload_but_the_detail_does():
    """Datensparsamkeit: eine Liste braucht keine Einzelheiten."""
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        await store.add_item({"notification_id": "pn-a", "task_id": "bt-a",
                              "run_id": "r", "priority": "normal",
                              "summary": "kurz", "findings": ["eins", "zwei"],
                              "fingerprint": "f", "content_trust": "untrusted_web"})
        center = _center(store)
        listed = (await center.inbox())["meldungen"][0]
        detail = await center.item("pn-a")
        return listed, detail
    listed, detail = _run(scenario())
    require("befunde" not in listed, "die Liste traegt keine Einzelheiten")
    require("befunde" in detail, "die Einzelansicht schon")
    require_equal(detail["herkunft"], "aus einer fremden Quelle",
                  "und benennt die Herkunft in Worten statt als Fachbegriff")
    require("untrusted" not in json.dumps(listed), "kein Fachbegriff in der Liste")


# -- Chronik -------------------------------------------------------------------

def t_the_timeline_is_a_projection_not_a_second_journal():
    raw = open(os.path.join(CONTROL_DIR, "activity.py"), encoding="utf-8").read()
    # Nach SCHREIB-SQL suchen, nicht nach Wortstaemmen: `created_at` ist ein
    # gelesener Spaltenname, und ein Test, der daran anschlaegt, zwingt nur zum
    # Umbenennen. SQL steht ausserdem in Zeichenketten — der tokenisierte
    # Vergleich taugt hier also gerade nicht.
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE",
                      "DROP TABLE", "ALTER TABLE"):
        require(forbidden not in raw.upper(),
                f"{forbidden}: die Chronik schreibt mit")
    require("mode=ro" in raw,
            "und liest den Freigabepfad ausdruecklich schreibgeschuetzt")
    # Und keine schreibenden Aufrufe der eigenen Speicher.
    code = _code_only("activity.py")
    for forbidden in ("put_task", "add_item", "finish_run", "mark_read",
                      "claim_run", "delete_task", "prune"):
        require(forbidden not in code, f"{forbidden}: die Chronik veraendert etwas")


def t_polling_noise_never_reaches_the_timeline():
    """Eine Beobachtung im Viertelstundentakt erzeugt am Tag 96 Laeufe ohne
    Aenderung. Als Chronikzeilen waeren sie unlesbar."""
    async def scenario():
        store = _store()
        now = time.time()
        await store.put_task(_task(store, task_id="bt-a", now=now, title="Wache"))
        for index in range(20):
            occurrence = now - index * 900
            run_id = f"r-{index}"
            await store.claim_run("bt-a", occurrence, run_id)
            await store.finish_run(run_id, S.NO_CHANGE, detail="unveraendert")
        return await A.timeline(proactive_store=store, now=now)
    events = _run(scenario())
    require(not any(e.kind == "lauf" for e in events),
            f"kein einziger 'nichts Neues'-Lauf: {[e.title for e in events][:3]}")
    require(any(e.kind == "aufgabe_angelegt" for e in events),
            "das Anlegen ist dagegen eine Zeile wert")


def t_repeated_identical_events_collapse():
    async def scenario():
        store = _store()
        now = time.time()
        await store.put_task(_task(store, task_id="bt-a", now=now, title="Wache"))
        for index in range(12):
            occurrence = now - index * 60
            run_id = f"r-{index}"
            await store.claim_run("bt-a", occurrence, run_id)
            await store.finish_run(run_id, S.DONE)
        return await A.timeline(proactive_store=store, now=now)
    events = _run(scenario())
    runs = [e for e in events if e.kind == "lauf"]
    require(len(runs) <= 2, f"zwoelf Laeufe in einer Stunde ergeben nicht zwoelf "
                            f"Zeilen ({len(runs)})")


def t_the_timeline_is_ordered_newest_first():
    async def scenario():
        store = _store()
        now = time.time()
        await store.put_task(_task(store, task_id="bt-a", now=now - 5000))
        for index, offset in enumerate((100, 4000, 2000)):
            run_id = f"r-{index}"
            await store.claim_run("bt-a", now - offset, run_id)
            await store.finish_run(run_id, S.FAILED, detail="kaputt")
        return await A.timeline(proactive_store=store, now=now)
    events = _run(scenario())
    stamps = [e.at for e in events]
    require_equal(stamps, sorted(stamps, reverse=True), "neueste zuerst")


def t_capability_names_become_words():
    require_equal(A.human("gmail_send_draft"), "E-Mail gesendet")
    require_equal(A.human("calendar_list_events"), "Kalender gelesen")
    require_equal(A.human("portal_login"), "Bei einem Portal angemeldet")
    require("_" not in A.human("etwas_unbekanntes"), "auch Unbekanntes ohne Unterstrich")


# -- Sicherheit -----------------------------------------------------------------

def t_every_route_demands_the_registered_device():
    """Es gibt keine Route ohne Pruefung — auch keine 'harmlose'."""
    source = open(os.path.join(CONTROL_DIR, "routes.py"), encoding="utf-8").read()
    handlers = [line for line in source.splitlines()
                if line.strip().startswith("async def ")
                and "request: web.Request" in line]
    require(len(handlers) >= 8, f"es gibt Routen ({len(handlers)})")
    # Jeder Handler prueft, bevor er etwas tut.
    for name in ("overview", "tasks", "task", "inbox", "item", "activity",
                 "system", "running"):
        block = source[source.index(f"async def {name}(request"):]
        block = block[:block.index("\n\n")]
        require("guard(request)" in block, f"{name} prueft den Anrufer nicht")
        require(block.index("guard(request)") < block.index("_center(request)")
                if "_center(request)" in block else True,
                f"{name} prueft NACH dem Lesen")


def t_the_check_is_the_gateways_own_not_a_copy():
    """Eine zweite Fassung derselben Pruefung ist die Stelle, an der spaeter eine
    von beiden nachgeschaerft wird und die andere nicht."""
    source = open(os.path.join(CONTROL_DIR, "routes.py"), encoding="utf-8").read()
    require("from solvio.security.mobile_approval.gateway import _authed_device"
            in source, "die Pruefung des Gateways wird aufgerufen")
    for forbidden in ("X-Device-Id", "X-Transport-Cred", "verify_transport_cred"):
        require(forbidden not in source, f"{forbidden} wird hier nachgebaut")


def t_the_control_center_can_never_approve():
    """Der schaerfste Test der Datei: es gibt keinen Weg von hier zu einer Freigabe."""
    code = (_code_only("routes.py") + " " + _code_only("snapshot.py")).lower()
    # Gesucht wird nach AUFRUFEN, die entscheiden — nicht nach dem Wortstamm.
    # `approver_runtime` enthaelt „approve" und ist trotzdem genau richtig: es
    # ist der lesende Zugriff auf die Zahl wartender Freigaben. Ein Test, der
    # daran anschlaegt, erzieht dazu, den Namen zu verstecken statt die Sache
    # zu pruefen.
    for forbidden in ("decision", "take_approved", "transition", "challenge",
                      "sign", "attest", "secure_enclave"):
        require(forbidden not in code,
                f"'{forbidden}' kommt als Bezeichner im Kontrollzentrum vor")
    # Positiv: der einzige Zugriff auf den Freigabepfad ist lesend.
    snapshot = open(os.path.join(CONTROL_DIR, "snapshot.py"),
                    encoding="utf-8").read()
    require("approvals.pending()" in snapshot, "gelesen wird die Warteschlange")
    require("approvals.approve" not in snapshot, "entschieden wird nichts")
    # Und positiv: die Handlungen, die es gibt, sind abschliessend aufgezaehlt.
    center_source = open(os.path.join(CONTROL_DIR, "routes.py"),
                         encoding="utf-8").read()
    block = center_source[center_source.index('handler = {'):]
    block = block[:block.index("}")]
    for allowed in ("pause", "resume", "run_now", "delete", "mark_read"):
        require(allowed in block, f"{allowed} fehlt")
    require(block.count(":") == 5, f"genau fuenf Handlungen, nicht mehr: {block}")


def t_no_secret_can_leave_through_a_snapshot():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        await store.add_item({"notification_id": "pn-a", "task_id": "bt-a",
                              "run_id": "r", "priority": "normal",
                              "summary": "x", "fingerprint": "f",
                              "content_trust": "untrusted_web"})
        center = _center(store, runtime=_Runtime())
        return json.dumps([await center.overview(), await center.tasks(),
                           await center.inbox(), await center.system(),
                           await center.activity()], ensure_ascii=False)
    blob = _run(scenario()).lower()
    for forbidden in ("token", "secret", "api_key", "apikey", "password",
                      "passwort", "credential", "hmac", "private", ".env",
                      "keychain", "bearer", "refresh_token"):
        require(forbidden not in blob, f"'{forbidden}' steht in einer Ansicht")


def t_no_implementation_trivia_reaches_the_screen():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a",
                                   last_error="capability_failed: "
                                              "credential_or_connection_missing:invalid_grant"))
        center = _center(store)
        return json.dumps(await center.tasks(), ensure_ascii=False)
    blob = _run(scenario())
    for forbidden in ("sqlite", "task_run_id", "PostureViolation", "traceback",
                      "capability_failed", "untrusted_executor", "/Users/"):
        require(forbidden not in blob, f"'{forbidden}' steht auf dem Bildschirm")
    require("neu anmelden" in blob, "stattdessen steht dort, was zu tun ist")


def t_the_routes_carry_no_body_and_take_no_free_text():
    """Handlungen tragen nur eine Kennung im Pfad. Kein Rumpf, keine Freitexte —
    also auch keine Stelle, an der etwas anderes mitkaeme."""
    source = open(os.path.join(CONTROL_DIR, "routes.py"), encoding="utf-8").read()
    require("await request.json()" not in source, "kein Rumpf wird gelesen")
    require("request.query" not in source, "keine Abfrageparameter")
    require("match_info" in source, "die Kennung kommt aus dem Pfad")


def t_repeated_snapshots_do_not_leak_database_connections():
    """Der Fehler, den erst die Live-Abnahme gefunden hat.

    `with sqlite3.connect(...) as c:` sieht aus wie ein Schliessen und ist eines
    NICHT — es schreibt die Transaktion fest und laesst die Verbindung offen.
    Ohne Last faellt das nie auf; unter dem Drei-Sekunden-Takt des
    Kontrollzentrums standen nach wenigen Minuten 227 offene Verbindungen auf
    dieselbe Datei, und SQLite gab mit `unable to open database file` auf.

    Der Test misst deshalb die offenen Deskriptoren des eigenen Prozesses, statt
    nur zu pruefen, dass ein Aufruf gelingt.
    """
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="bt-a"))
        await store.add_item({"notification_id": "pn-a", "task_id": "bt-a",
                              "run_id": "r", "priority": "normal",
                              "summary": "x", "fingerprint": "f",
                              "content_trust": ""})
        center = _center(store)
        for _ in range(60):
            await center.overview()
            await center.tasks()
            await center.inbox()
        return store.path
    path = _run(scenario())

    import subprocess
    out = subprocess.run(["/usr/sbin/lsof", "-p", str(os.getpid())],
                         capture_output=True, text=True, timeout=30).stdout
    open_handles = sum(1 for line in out.splitlines() if path in line)
    require(open_handles <= 4,
            f"nach 180 Abfragen stehen {open_handles} Verbindungen offen")


def t_the_store_closes_what_it_opens():
    """Strukturell, damit die Regel nicht nur an einer Messung haengt."""
    raw = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                            "proactive", "store.py"), encoding="utf-8").read()
    require("with self._connect() as connection:" not in raw,
            "der Transaktions-Kontext wird nicht mehr fuer ein Schliessen gehalten")
    require("connection.close()" in raw, "es wird wirklich geschlossen")
    require("contextlib.contextmanager" in raw,
            "und zwar an einer Stelle, nicht sechzehnmal von Hand")


# -- Degradiert -----------------------------------------------------------------

def t_a_missing_store_gives_empty_views_not_an_error():
    async def scenario():
        center = _center(None)
        return (await center.tasks(), await center.inbox(),
                await center.overview())
    tasks, inbox, overview = _run(scenario())
    require_equal(tasks["aufgaben"], [], "leer statt kaputt")
    require_equal(inbox["meldungen"], [], "auch hier")
    require_equal(overview["aufgaben_gesamt"], 0, "und die Uebersicht steht trotzdem")


def t_a_missing_approver_does_not_break_the_overview():
    async def scenario():
        return await _center(_store(), runtime=None).overview()
    overview = _run(scenario())
    require_equal(overview["freigaben_offen"], 0, "keine Freigaben bekannt")
    require(overview.get("stand"), "die Uebersicht steht trotzdem")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
