"""Arbeiten, wenn niemand da ist — ohne dabei Vollmachten zu erfinden.

Zwei Zusagen tragen diesen Meilenstein, und beide sind leicht zu behaupten und
schwer zu halten.

**Genau einmal.** Nicht „meistens einmal". Ein Neustart mitten im Lauf, zwei
Ticks kurz hintereinander, eine lange Ausfuehrung, die den naechsten Termin
ueberholt — jeder dieser Faelle erzeugt sonst einen zweiten Lauf, und beim
zweiten Lauf ist die Mail schon geschrieben. Die Zusage steckt deshalb nicht in
Anwendungslogik, sondern in einer eindeutigen Spalte: wer einfuegen kann,
besitzt die Gelegenheit.

**Planen ist keine Vollmacht.** „Der Nutzer hat das doch am Dienstag erlaubt" ist
die Formulierung, mit der aus einer Bitte eine dauerhafte Befugnis wird. Ein
geplanter Schreibvorgang laeuft deshalb durch dieselbe Freigabe wie ein
gesprochener — und wenn niemand bestaetigt, passiert nichts. Dazu gehoert auch:
danach wird kein anderer Weg gesucht.
"""
import asyncio
import inspect
import os
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.capabilities.contract import CapabilityDeclined  # noqa: E402
from solvio.capabilities.envelope import CapabilityOutcome as OUT, CapabilityResult  # noqa: E402
from solvio.capabilities.proactive import (PREAUTHORIZABLE,  # noqa: E402
    ALLOWED_ACTIONS, SPECS, ProactiveCapabilities, ZUSTELLUNG, parse_when,
)
from solvio.contracts.trust import TrustLevel, is_untrusted  # noqa: E402
from solvio.proactive import fingerprint as FP  # noqa: E402
from solvio.proactive import store as S  # noqa: E402
from solvio.proactive.runner import (  # noqa: E402
    MAX_FAILURES, TaskRunner, backoff_after, background_trust, classify_failure,
    run_id_for,
)
from solvio.proactive.schedule import (  # noqa: E402
    DEFAULT_TZ, MIN_INTERVAL_SECONDS, Kind, Schedule, ScheduleError,
    clamp_interval, daily, every, in_seconds, weekly,
)
from solvio.proactive.scheduler import MAX_CONCURRENT, Scheduler  # noqa: E402

BER = ZoneInfo("Europe/Berlin")
PROACTIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                             "proactive")


def _run(coro):
    return asyncio.run(coro)


def _at(year, month, day, hour, minute=0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=BER).timestamp()


def _code_only(path: str) -> str:
    """Quelltext ohne Kommentare und Zeichenketten.

    Diese Dateien ERKLAEREN, warum es kein Cron-Feld gibt. Eine Wortsuche ueber
    den Rohtext schlaegt deshalb bei genau der Datei an, die es richtig macht.
    """
    import io
    import tokenize
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return " ".join(kept)


def _store() -> S.ProactiveStore:
    return S.ProactiveStore(os.path.join(tempfile.mkdtemp(), "p.sqlite3"))


class _Router:
    """Ein Router, der genau das antwortet, was der Test braucht."""

    def __init__(self, data=None, outcome=OUT.SUCCESS, reason=""):
        self.data = data if data is not None else {"termine": []}
        self.outcome = outcome
        self.reason = reason
        self.calls: list[tuple[str, dict]] = []
        self.trusts: list = []

    def names(self):
        return ["calendar_list_events", "deep_research", "deep_task_status",
                "deep_cancel", "gmail_send_draft"]

    async def execute(self, name, arguments=None, **kwargs):
        self.calls.append((name, dict(arguments or {})))
        self.trusts.append(kwargs.get("trust"))
        payload = self.data() if callable(self.data) else self.data
        return CapabilityResult(self.outcome, f"c{len(self.calls)}", name,
                                data=payload, reason=self.reason)


class _Dispatcher:
    def __init__(self, router):
        self.capabilities = router


def _task(store, **overrides) -> S.Task:
    now = overrides.pop("now", time.time())
    task = S.Task(
        task_id=overrides.pop("task_id", "t-1"), owner="gregor",
        title=overrides.pop("title", "Test"), created_at=now,
        created_from=overrides.pop("created_from", "vom Nutzer"),
        schedule=overrides.pop("schedule", daily(7, 30).as_dict()),
        action=overrides.pop("action", {"kind": "capability",
                                        "capability": "calendar_list_events",
                                        "arguments": {}, "notify": "immer"}),
        next_run_at=overrides.pop("next_run_at", now - 1))
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


# -- Zeitplan ----------------------------------------------------------------

def t_a_daily_task_keeps_its_wall_clock_across_the_spring_change():
    """Die Umstellung ist der Grund, warum hier keine 86400 Sekunden stehen."""
    plan = daily(7, 30)
    before = plan.next_after(_at(2026, 3, 28, 20))
    across = plan.next_after(before)
    after = plan.next_after(across)
    for stamp in (before, across, after):
        local = datetime.fromtimestamp(stamp, BER)
        require_equal((local.hour, local.minute), (7, 30),
                      f"7:30 blieb nicht 7:30 am {local:%d.%m.}")
    require(abs((after - across) - 86400) < 1,
            "nach der Umstellung stimmt der Tagesabstand wieder")


def t_a_daily_task_keeps_its_wall_clock_across_the_autumn_change():
    plan = daily(7, 30)
    # Der lange Tag liegt zwischen dem 24. und dem 25. — die Uhr wird in der
    # Nacht dazwischen zurueckgestellt. Ein Startpunkt am Abend des 24. haette
    # das Paar verfehlt und harmlos 24 Stunden gemessen.
    first = plan.next_after(_at(2026, 10, 23, 20))
    second = plan.next_after(first)
    third = plan.next_after(second)
    require_equal(datetime.fromtimestamp(first, BER).day, 24, "erst der 24.")
    require(abs((second - first) - 25 * 3600) < 1,
            f"der Tag mit 25 Stunden wurde nicht erkannt "
            f"({(second - first) / 3600:.1f} h)")
    require(abs((third - second) - 86400) < 1, "danach wieder 24 Stunden")
    for stamp in (first, second, third):
        local = datetime.fromtimestamp(stamp, BER)
        require_equal((local.hour, local.minute), (7, 30), "Wanduhr bleibt 7:30")


def t_an_hour_that_does_not_exist_moves_forward_instead_of_vanishing():
    """2:30 gibt es am Umstellungstag im Fruehjahr nicht.

    Der naive Rechner liefert dort entweder nichts — die Aufgabe faellt still
    einen Tag aus — oder einen Zeitpunkt, dessen Rueckrechnung eine andere
    Uhrzeit ergibt.
    """
    plan = daily(2, 30)
    moment = plan.next_after(_at(2026, 3, 28, 12))
    local = datetime.fromtimestamp(moment, BER)
    require_equal((local.month, local.day), (3, 29), "es bleibt derselbe Tag")
    require(local.hour >= 3, f"auf den ersten gueltigen Augenblick gelegt ({local})")
    require(moment > _at(2026, 3, 28, 12), "und liegt in der Zukunft")


def t_an_hour_that_happens_twice_is_taken_once():
    plan = daily(2, 30)
    first = plan.next_after(_at(2026, 10, 24, 12))
    second = plan.next_after(first)
    require(second - first > 24 * 3600 - 1,
            "die doppelte Stunde erzeugte keinen zweiten Lauf am selben Tag")


def t_weekly_hits_only_the_named_days():
    plan = weekly((0, 3), 8, 0)
    moment = _at(2026, 8, 23, 12)      # ein Sonntag
    seen = []
    for _ in range(4):
        moment = plan.next_after(moment)
        seen.append(datetime.fromtimestamp(moment, BER).weekday())
    require_equal(seen, [0, 3, 0, 3], f"falsche Wochentage: {seen}")


def t_next_after_is_strict_so_a_task_cannot_loop():
    """Nicht-streng waere eine Endlosschleife: dieselbe Gelegenheit immer wieder."""
    plan = daily(7, 30)
    moment = plan.next_after(_at(2026, 8, 23, 0))
    require(plan.next_after(moment) > moment, "die naechste liegt echt spaeter")


def t_a_one_shot_has_exactly_one_occurrence():
    now = time.time()
    plan = in_seconds(120, now=now)
    first = plan.next_after(now)
    require(first is not None, "einmal ist sie faellig")
    require(plan.next_after(first) is None, "und danach nie wieder")


def t_a_watch_interval_below_the_floor_is_refused_not_silently_raised():
    """Der erste Entwurf hat still hochgesetzt — und niemand haette es erfahren."""
    try:
        every(30)
        require(False, "ein zu kurzer Abstand haette auffallen muessen")
    except ScheduleError as exc:
        require("Untergrenze" in str(exc), "und zwar mit Begruendung")
    seconds, note = clamp_interval(30)
    require_equal(seconds, MIN_INTERVAL_SECONDS, "an der Oberflaeche gehoben")
    require(note, "und der Nutzer erfaehrt es")
    require_equal(clamp_interval(900), (900, ""), "ein passender Abstand bleibt")


def t_a_schedule_survives_a_round_trip_through_the_database():
    for plan in (daily(7, 30), weekly((0, 4), 8, 15), every(600),
                 in_seconds(60, now=time.time())):
        again = Schedule.from_dict(plan.as_dict())
        require_equal(again.kind, plan.kind, "Art bleibt")
        require_equal(again.timezone, DEFAULT_TZ, "Zeitzone bleibt")
        moment = time.time()
        require_equal(again.next_after(moment), plan.next_after(moment),
                      f"{plan.kind.value}: andere naechste Gelegenheit")


# -- Verpasste Gelegenheiten -------------------------------------------------

def t_two_weeks_of_downtime_produce_at_most_one_run():
    """Siebzehn Morgenberichte nachzuholen waere keine Sorgfalt, sondern Strafe."""
    plan = daily(7, 30)
    missed = _at(2026, 8, 9, 7, 30)
    due, skipped = plan.catch_up(missed, _at(2026, 8, 23, 8, 15))
    require(due is not None, "einer wird nachgeholt")
    require_equal(datetime.fromtimestamp(due, BER).day, 23, "und zwar der heutige")
    require(skipped >= 13, f"die uebrigen werden gezaehlt, nicht ausgefuehrt ({skipped})")


def t_a_missed_run_that_no_longer_matters_is_skipped_truthfully():
    plan = daily(7, 30)
    due, skipped = plan.catch_up(_at(2026, 8, 9, 7, 30), _at(2026, 8, 23, 22, 0))
    require(due is None, "um 22 Uhr ist ein Morgenbericht sinnlos")
    require(skipped > 0, "aber es wird gezaehlt, nicht verschwiegen")


def t_recovery_marks_an_interrupted_run_as_unknown_not_as_success():
    async def scenario():
        store = _store()
        router = _Router()
        await store.put_task(_task(store, task_id="t-x"))
        await store.claim_run("t-x", 1000.0, "br-halb")
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01)
        report = await scheduler.recover()
        runs = await store.runs_for("t-x")
        return report, runs, router
    report, runs, router = _run(scenario())
    require_equal(report["unterbrochen"], 1, "der angebrochene Lauf wurde erkannt")
    require_equal(runs[0]["state"], S.FAILED, "und nicht als Erfolg verbucht")
    require("unbekannt" in (runs[0]["detail"] or ""), "der Ausgang bleibt offen")
    require_equal(router.calls, [], "und er wird nicht blind wiederholt")


# -- Genau einmal ------------------------------------------------------------

def t_the_same_occurrence_can_only_be_claimed_once():
    """Die Zusage kommt aus der Datenbank, nicht aus einer Prüfung davor."""
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="t-c"))
        first = await store.claim_run("t-c", 1234.0, run_id_for("t-c", 1234.0))
        second = await store.claim_run("t-c", 1234.0, run_id_for("t-c", 1234.0))
        third = await store.claim_run("t-c", 1234.0, "eine-andere-kennung")
        return first, second, third
    first, second, third = _run(scenario())
    require(first, "der erste bekommt sie")
    require(not second, "der zweite nicht")
    require(not third, "auch nicht unter anderem Namen — die Gelegenheit zaehlt")


def t_a_run_id_is_derived_so_a_restart_recognises_it():
    """Zufaellige Kennungen waeren nach einem Neustart neu — und damit wertlos."""
    require_equal(run_id_for("t-1", 1000.0), run_id_for("t-1", 1000.0),
                  "gleiche Gelegenheit, gleiche Kennung")
    require(run_id_for("t-1", 1000.0) != run_id_for("t-1", 1001.0),
            "andere Gelegenheit, andere Kennung")
    require(run_id_for("t-1", 1000.0) != run_id_for("t-2", 1000.0),
            "andere Aufgabe, andere Kennung")
    require_equal(run_id_for("t-1", 1000.0), run_id_for("t-1", 1000.4),
                  "Sekundenbruchteile sind keine neue Gelegenheit")


def t_a_second_tick_does_not_start_the_same_task_again():
    async def scenario():
        store = _store()
        router = _Router({"termine": [{"id": "e1"}]})
        now = time.time()
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now)
        await store.put_task(_task(store, task_id="t-d", now=now,
                                   schedule=in_seconds(1, now=now - 10).as_dict(),
                                   next_run_at=now - 1))
        await scheduler.poll()
        await asyncio.sleep(0.3)
        again = await scheduler.poll()
        await asyncio.sleep(0.3)
        return again, router, await store.unread_count()
    again, router, items = _run(scenario())
    require_equal(again, 0, "beim zweiten Tick ist nichts mehr faellig")
    require_equal(len(router.calls), 1, "die Faehigkeit lief genau einmal")
    require_equal(items, 1, "und es gibt genau eine Meldung")


def t_a_long_run_is_not_overtaken_by_the_next_tick():
    async def scenario():
        store = _store()
        now = time.time()
        router = _Router({"termine": []})

        slow = asyncio.Event()

        async def blocking(name, arguments=None, **kwargs):
            router.calls.append((name, dict(arguments or {})))
            await slow.wait()
            return CapabilityResult(OUT.SUCCESS, "c", name, data={"termine": []})

        router.execute = blocking
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now)
        await store.put_task(_task(store, task_id="t-slow", now=now,
                                   schedule=every(300).as_dict(),
                                   next_run_at=now - 1))
        await scheduler.poll()
        await asyncio.sleep(0.2)
        second = await scheduler.poll()
        await asyncio.sleep(0.2)
        slow.set()
        await asyncio.sleep(0.2)
        return second, len(router.calls)
    second, calls = _run(scenario())
    require_equal(second, 0, "waehrend ein Lauf laeuft, startet keiner nach")
    require_equal(calls, 1, "und die Faehigkeit lief nur einmal")


# -- Aenderungserkennung -----------------------------------------------------

def t_identical_results_do_not_produce_a_second_notice():
    async def scenario():
        store = _store()
        now = [time.time()]
        router = _Router({"termine": [{"id": "e1", "titel": "Zahnarzt",
                                       "start": 1756000000}]})
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now[0])
        await store.put_task(_task(store, task_id="t-w", now=now[0],
                                   schedule=every(300).as_dict(),
                                   next_run_at=now[0] - 1,
                                   action={"kind": "capability",
                                           "capability": "calendar_list_events",
                                           "arguments": {},
                                           "notify": "bei_aenderung"}))
        counts = []
        for _ in range(3):
            await scheduler.poll()
            await asyncio.sleep(0.25)
            counts.append(await store.unread_count())
            now[0] += 301
            task = await store.get_task("t-w")
            task.next_run_at = now[0] - 1
            await store.put_task(task)
        router.data = {"termine": [{"id": "e1", "titel": "Zahnarzt",
                                    "start": 1756000000},
                                   {"id": "e2", "titel": "Neu", "start": 1}]}
        await scheduler.poll()
        await asyncio.sleep(0.25)
        counts.append(await store.unread_count())
        return counts
    counts = _run(scenario())
    require_equal(counts[0], 1, "der erste Lauf meldet")
    require_equal(counts[1], 1, "der zweite mit gleichem Ergebnis nicht")
    require_equal(counts[2], 1, "der dritte auch nicht")
    require_equal(counts[3], 2, "eine echte Aenderung meldet wieder")


def t_an_unchanged_run_is_recorded_as_no_change_not_as_a_result():
    """Der Laeufer entscheidet, ob es eine Neuigkeit war — nicht die Datenbank.

    Die Datenbank verhindert doppelte Meldungen ohnehin ueber den Fingerabdruck.
    Deshalb kam eine Mutation durch, die den Vergleich im Laeufer entfernte: der
    Posteingang sah gleich aus. Der LAUFZUSTAND aber nicht — und an ihm haengt,
    ob der Nutzer spaeter erfaehrt, dass nachgesehen wurde und nichts war.
    """
    async def scenario():
        store = _store()
        now = [time.time()]
        router = _Router({"termine": [{"id": "e1", "start": 1.0}]})
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now[0])
        await store.put_task(_task(store, task_id="t-nc", now=now[0],
                                   schedule=every(300).as_dict(),
                                   next_run_at=now[0] - 1,
                                   action={"kind": "capability",
                                           "capability": "calendar_list_events",
                                           "arguments": {},
                                           "notify": "bei_aenderung"}))
        states = []
        for _ in range(2):
            await scheduler.poll()
            await asyncio.sleep(0.25)
            runs = await store.runs_for("t-nc")
            states.append(runs[0]["state"])
            now[0] += 301
            task = await store.get_task("t-nc")
            task.next_run_at = now[0] - 1
            await store.put_task(task)
        return states
    states = _run(scenario())
    require_equal(states[0], S.DONE, "der erste Lauf ist ein Ergebnis")
    require_equal(states[1], S.NO_CHANGE,
                  "der zweite mit gleichem Ergebnis ist ausdruecklich keines")


def t_an_empty_result_still_says_something():
    """„Keine Termine" ist eine Auskunft. Ein blosser Titel ist keine.

    Im echten Abnahmelauf hatte der Kalender null Eintraege und die Meldung
    lautete nur „Kalender heute" — das liest sich wie ein Fehlschlag, obwohl es
    die richtige Antwort war.
    """
    from solvio.proactive.runner import _summarize
    task = _task(_store(), title="Kalender heute")
    leer = {"count": 0, "events": [], "content_trust": "untrusted_document"}
    summary = _summarize(task, leer)
    require(summary != task.title, "der Titel allein reicht nicht")
    require("nichts" in summary.lower() or "keine" in summary.lower(),
            f"es wird gesagt, dass nichts da war: {summary!r}")
    voll = {"count": 2, "events": [{"id": "a"}, {"id": "b"}]}
    require("2" in _summarize(task, voll), "und bei Inhalt wird gezaehlt")


def t_a_fingerprint_ignores_noise_but_not_substance():
    """Das gefaehrliche Rauschen sind Felder, die AUSSEHEN wie eine Kennung.

    `call_id` und `run_id` enden auf `_id` und werden deshalb als bezeichnend
    eingesammelt — obwohl sie sich bei jedem Lauf aendern. Ohne die
    Ausschlussliste meldete eine Beobachtung dadurch bei jedem Nachsehen eine
    Neuigkeit. Genau dieser Fall stand im ersten Test nicht drin, und eine
    Mutation, die den Ausschluss entfernte, kam durch.
    """
    base = {"termine": [{"id": "a", "start": 100.0}], "abgerufen": 1.0,
            "call_id": "c-111", "run_id": "r-111"}
    noise = {"termine": [{"id": "a", "start": 100.4}], "abgerufen": 999.0,
             "call_id": "c-222", "run_id": "r-222"}
    moved = {"termine": [{"id": "a", "start": 200.0}]}
    require_equal(FP.of(base), FP.of(noise), "Abrufzeit ist keine Aenderung")
    require(FP.of(base) != FP.of(moved), "ein verschobener Termin schon")
    require(FP.of(base).startswith("id:"), "bei Kennungen wird an ihnen gemessen")
    require(FP.of("nur Prosa").startswith("txt:"), "sonst am normalisierten Text")


def t_prose_reformatting_is_not_news_but_a_new_statement_is():
    require_equal(FP.of("Nichts Neues zu X."), FP.of("nichts neues zu x!  \n"),
                  "andere Schreibweise ist keine Neuigkeit")
    require(FP.of("Nichts Neues zu X.") != FP.of("Zu X gibt es eine Neuerung."),
            "eine andere Aussage schon")


def t_the_inbox_refuses_a_duplicate_at_the_database_level():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="t-i"))
        item = {"notification_id": "pn-1", "task_id": "t-i", "run_id": "r1",
                "priority": "normal", "summary": "etwas", "fingerprint": "f1",
                "content_trust": ""}
        first = await store.add_item(item)
        second = await store.add_item({**item, "notification_id": "pn-2",
                                       "run_id": "r2"})
        return first, second, await store.unread_count()
    first, second, count = _run(scenario())
    require(first, "die erste Meldung entsteht")
    require(not second, "dieselbe Erkenntnis erzeugt keine zweite")
    require_equal(count, 1, "und es bleibt bei einer")


# -- Autoritaet --------------------------------------------------------------

def t_a_background_run_carries_an_honest_note():
    """Ein Journal, das einen Hintergrundlauf als Sprachsitzung ausweist, ist
    genau dann wertlos, wenn man es braucht."""
    trust = background_trust(time.time())
    require_equal(trust.origin_trust, TrustLevel.USER_DIRECT, "vom Nutzer veranlasst")
    require(trust.user_authorized, "er hat die Aufgabe wirklich angelegt")
    require("background" in trust.note.lower(), "und die Notiz sagt es")
    require("voice" not in trust.note.lower(), "keine erfundene Sprachsitzung")
    require(not is_untrusted(trust.origin_trust), "und keine fremde Herkunft")


def t_a_scheduled_write_still_needs_the_phone():
    """Der Kern dieses Meilensteins: planen ist keine Vollmacht."""
    async def scenario():
        store = _store()
        router = _Router({}, outcome=OUT.APPROVAL_REQUIRED,
                         reason="awaiting_user_approval")
        now = time.time()
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now)
        await store.put_task(_task(
            store, task_id="t-write", now=now, next_run_at=now - 1,
            action={"kind": "capability", "capability": "gmail_send_draft",
                    "arguments": {"id": "x"}, "notify": "immer"}))
        await scheduler.poll()
        await asyncio.sleep(0.3)
        return (await store.runs_for("t-write"), await store.unread_count(),
                await store.get_task("t-write"), router)
    runs, items, task, router = _run(scenario())
    require_equal(runs[0]["state"], S.APPROVAL_PENDING, "die Gelegenheit wartet")
    require_equal(items, 0, "es entsteht keine Meldung ueber einen Erfolg")
    require("Freigabe" in task.last_error, "und die Aufgabe sagt, worauf sie wartet")
    require_equal(len(router.calls), 1,
                  "es wird GENAU EIN Versuch gemacht — kein zweiter Weg gesucht")


def t_the_runner_never_looks_for_another_route_around_an_approval():
    """Strukturell: der Laeufer kennt weder Resolver noch Fachteam."""
    source = open(os.path.join(PROACTIVE_DIR, "runner.py"),
                  encoding="utf-8").read()
    for forbidden in ("gap_resolver", "SpecialistTeam", "resolve(", "consult("):
        require(forbidden not in source, f"{forbidden} im Hintergrundlauf")
    scheduler = open(os.path.join(PROACTIVE_DIR, "scheduler.py"),
                     encoding="utf-8").read()
    for forbidden in ("gap_resolver", "SpecialistTeam", "consult("):
        require(forbidden not in scheduler, f"{forbidden} im Scheduler")


def t_only_reading_capabilities_may_be_scheduled():
    """Schreiben im Hintergrund gibt es nur als gebundene Haustechnik.

    Seit Approval Policy V2 (ADR-0022) steht gewoehnliche Haustechnik auf der
    Liste — aber NICHT als Vollmacht: ohne eine an ihre Wirkung gebundene
    Erlaubnis kostet auch sie jeden Lauf eine Freigabe. Alles andere
    Schreibende bleibt draussen: eine geplante Mail, ein geplanter Termin, eine
    geplante Loeschung haben hier weiterhin nichts verloren."""
    async def scenario():
        capabilities = ProactiveCapabilities(_store())
        try:
            await capabilities.create({"titel": "Mail senden",
                                       "wann": "taeglich 07:30",
                                       "aktion": "gmail_send_draft"})
            return None
        except CapabilityDeclined as exc:
            return exc
    declined = _run(scenario())
    require(declined is not None, "eine schreibende Faehigkeit wird abgelehnt")
    require_equal(declined.reason, "action_not_allowed", "und zwar benannt")
    for name in ALLOWED_ACTIONS:
        if name in PREAUTHORIZABLE:
            continue   # gewoehnliche Haustechnik, nur mit gebundener Erlaubnis
        require(not name.startswith(("gmail_send", "gmail_create",
                                     "calendar_create", "calendar_update",
                                     "calendar_delete", "ha_turn", "ha_set",
                                     "portal_")),
                f"{name} ist schreibend und steht doch auf der Liste")


def t_creating_a_task_does_not_carry_the_authority_of_what_it_will_do():
    """`background_create` schreibt in SOLVIOs eigene Datenbank — mehr nicht."""
    require_equal(SPECS["background_create"].base_risk.name, "HARMLESS",
                  "das Anlegen selbst ist harmlos")
    require(SPECS["background_create"].semantics != "NON_IDEMPOTENT_WRITE",
            "es veraendert nichts in der Welt")
    require_equal(SPECS["proactive_list"].base_risk.name, "HARMLESS", "Lesen")
    require(SPECS["proactive_list"].is_read_only(), "und zwar wirklich lesend")


# -- Fremder Inhalt ----------------------------------------------------------

def t_external_content_cannot_create_a_task():
    """Eine Mail mit „richte das ein" ist Information, kein Auftrag."""
    from solvio.capabilities.contract import authority_refusal
    from solvio.contracts.trust import TrustContext
    spec = SPECS["background_create"]
    for level in (TrustLevel.UNTRUSTED_EMAIL, TrustLevel.UNTRUSTED_WEB,
                  TrustLevel.UNTRUSTED_DOCUMENT, TrustLevel.UNTRUSTED_MESSAGE):
        trust = TrustContext(origin_trust=level, user_authorized=False,
                             note="fremder Inhalt")
        from solvio.capabilities.contract import RiskLevel
        refusal = authority_refusal(spec, trust, RiskLevel.MUTATING)
        require_equal(refusal, "untrusted_origin",
                      f"{level.value} darf keine Aufgabe anlegen")


def t_a_model_invented_task_has_no_authority_either():
    from solvio.capabilities.contract import RiskLevel, authority_refusal
    from solvio.contracts.trust import TrustContext
    trust = TrustContext(origin_trust=TrustLevel.AGENT_GENERATED,
                         user_authorized=False, note="selbst ausgedacht")
    require_equal(authority_refusal(SPECS["background_create"], trust,
                                    RiskLevel.MUTATING), "no_user_authority",
                  "auch das Modell kann sich nicht selbst beauftragen")


def t_the_stored_task_records_the_humans_own_words():
    """Damit spaeter nachvollziehbar ist, worauf die Automatik zurueckgeht.

    Und zwar ueber den ROUTER, nicht am Handler vorbei. Der erste Entwurf reichte
    Herkunft als `_user_text` durch die Argumente — was am Schema-Pruefer
    scheiterte (`unknown_argument`). Meine Tests riefen den Handler direkt auf und
    haben den ganzen Pfad deshalb nie beruehrt; gefunden hat es erst der
    Live-Lauf. Dieser Test geht jetzt den vollen Weg.
    """
    async def scenario():
        from solvio.capabilities.invocation import CapabilityInvocationGate, voice_trust
        from solvio.capabilities.router import CapabilityRouter
        from solvio.capabilities.proactive import register as register_proactive
        store = _store()
        gate = CapabilityInvocationGate()
        # Bewusst ein Satz, in dem jedes Argument woertlich vorkommt: sonst hebt
        # `effective_risk` den Aufruf an und der Test scheiterte an der fehlenden
        # Freigabe statt am Speicherweg, den er pruefen soll.
        spoken = ("Leg mir Morgenkalender an, taeglich 07:30, mit "
                  "calendar_list_events fuer heute.")
        gate.begin_turn(session_id="s", turn_id="t", principal="pi-wohnzimmer",
                        trust=voice_trust(True), user_text=spoken)
        router = CapabilityRouter()
        register_proactive(router, ProactiveCapabilities(store, gate=gate))
        context = gate.context()
        result = await router.execute(
            "background_create",
            {"titel": "Morgenkalender", "wann": "taeglich 07:30",
             "aktion": "calendar_list_events", "argumente": {"when": "heute"}},
            trust=context.trust,
            provenance=gate.provenance_for(
                {"titel": "Morgenkalender", "wann": "taeglich 07:30",
                 "aktion": "calendar_list_events"}),
            principal=context.principal)
        tasks = await store.list_tasks()
        return result, tasks, spoken
    result, tasks, spoken = _run(scenario())
    require(result.succeeded, f"der volle Weg traegt: {result.reason}")
    require_equal(len(tasks), 1, "genau eine Aufgabe entstand")
    require_equal(tasks[0].created_from, spoken,
                  "im Wortlaut des Menschen, aus dem Gate")
    require_equal(tasks[0].owner, "pi-wohnzimmer", "und mit dem echten Auftraggeber")
    source = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                               "tools", "proactive_capability_tools.py"),
                  encoding="utf-8").read()
    require("_user_text" not in source,
            "Herkunft wandert nicht durch die Argumente")


def t_creating_a_task_needs_a_confirmation_when_the_model_supplied_the_wording():
    """Beobachtetes Verhalten des Vertrags — hier festgehalten, nicht geglaettet.

    `background_create` schreibt, und `effective_risk()` hebt einen schreibenden
    Aufruf um eine Stufe, sobald ein Argument nicht im Gesagten vorkommt. Der
    Faehigkeitsname (`calendar_list_events`) wird nie woertlich gesagt — also
    braucht das Anlegen in der Praxis eine Freigabe.

    Das ist unbequem und trotzdem richtig herum: eine dauerhafte Automatik ist
    folgenreich, und die Alternative waere, `effective_risk` aufzuweichen. Der
    Test haelt beide Zweige fest, damit niemand spaeter das eine fuer einen
    Fehler haelt und das andere still abschafft.
    """
    async def scenario(spoken, arguments):
        from solvio.capabilities.invocation import CapabilityInvocationGate, voice_trust
        from solvio.capabilities.contract import effective_risk, requires_approval
        from solvio.capabilities.router import CapabilityRouter
        from solvio.capabilities.proactive import register as register_proactive
        gate = CapabilityInvocationGate()
        gate.begin_turn(session_id="s", turn_id="t", principal="pi",
                        trust=voice_trust(True), user_text=spoken)
        router = CapabilityRouter()
        register_proactive(router, ProactiveCapabilities(_store(), gate=gate))
        provenance = gate.provenance_for(arguments)
        risk = effective_risk(SPECS["background_create"], provenance)
        return provenance, risk, requires_approval(risk)

    # Alles gesagt: keine Anhebung.
    spoken_all = "Kalender heute taeglich 07:30 calendar_list_events"
    provenance, risk, needs = _run(scenario(
        spoken_all, {"titel": "Kalender heute", "wann": "taeglich 07:30",
                     "aktion": "calendar_list_events"}))
    require(all(p.value == "user_direct" for p in provenance.values()),
            f"alles Gesagte gilt als vom Nutzer: {provenance}")
    require(not needs, "dann braucht es keine Freigabe")

    # Der uebliche Fall: der Faehigkeitsname faellt nie im Gespraech.
    provenance, risk, needs = _run(scenario(
        "Pruef mir jeden Morgen meinen Kalender.",
        {"titel": "Morgenkalender", "wann": "taeglich 07:30",
         "aktion": "calendar_list_events"}))
    require(any(p.value == "model_derived" for p in provenance.values()),
            "der Faehigkeitsname stammt vom Modell")
    require(needs, "und damit braucht das Anlegen eine Freigabe")
    require_equal(risk.name, "MUTATING", "genau eine Stufe hoeher, nicht mehr")


def t_research_results_keep_their_foreign_rank():
    async def scenario():
        store = _store()
        router = _Router()
        calls = {"n": 0}

        async def deep(name, arguments=None, **kwargs):
            calls["n"] += 1
            if name == "deep_research":
                return CapabilityResult(OUT.SUCCESS, "c", name,
                                        data={"task_id": "d1", "status": "succeeded",
                                              "ergebnis": {"zusammenfassung": "etwas"},
                                              "content_trust": "untrusted_executor"})
            return CapabilityResult(OUT.SUCCESS, "c", name, data={})

        router.execute = deep
        runner = TaskRunner(_Dispatcher(router), store)
        task = _task(store, action={"kind": "research", "topic": "ein Thema"})
        return await runner.execute(task, time.time(), "r1")
    outcome = _run(scenario())
    require_equal(outcome.state, S.DONE, "die Recherche lief")
    require_equal(outcome.item["content_trust"], "untrusted_executor",
                  "und bleibt fremde Information")


# -- Fehler und Rueckzug ------------------------------------------------------

def t_failures_are_classified_the_same_way_the_resolver_does():
    require_equal(classify_failure(OUT.CAPABILITY_FAILED, "credentials_missing"),
                  "credential_or_connection_missing",
                  "ein abgelaufener Zugang ist keine fehlende Faehigkeit")
    require_equal(classify_failure(OUT.CAPABILITY_FAILED, "invalid_grant"),
                  "credential_or_connection_missing", "auch bei OAuth-Wortlaut")
    require_equal(classify_failure(OUT.EXECUTOR_UNAVAILABLE, "executor_unavailable"),
                  "device_or_service_unavailable", "eine Stoerung ist eine Stoerung")
    require_equal(classify_failure(OUT.TIMEOUT, "timeout"),
                  "device_or_service_unavailable", "eine Frist auch")
    require_equal(classify_failure(OUT.APPROVAL_REQUIRED, "awaiting_user_approval"),
                  "approval_required", "und eine Freigabe ist eine Freigabe")


def t_backoff_grows_and_is_capped():
    previous = 0.0
    for failures in range(1, 12):
        wait = backoff_after(failures)
        require(wait >= previous, "der Abstand wird nie kuerzer")
        require(wait <= 6 * 3600, f"und nie groesser als sechs Stunden ({wait})")
        previous = wait
    require(backoff_after(1) < backoff_after(4), "er waechst wirklich")


def t_a_provider_outage_pauses_the_task_instead_of_hammering():
    async def scenario():
        store = _store()
        router = _Router({}, outcome=OUT.EXECUTOR_UNAVAILABLE,
                         reason="executor_unavailable")
        now = [time.time()]
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now[0])
        await store.put_task(_task(store, task_id="t-out", now=now[0],
                                   schedule=every(300).as_dict(),
                                   next_run_at=now[0] - 1))
        for _ in range(MAX_FAILURES + 2):
            task = await store.get_task("t-out")
            if not task.enabled:
                break
            task.next_run_at = now[0] - 1
            task.retry_after = None
            await store.put_task(task)
            await scheduler.poll()
            await asyncio.sleep(0.2)
            now[0] += 400
        return await store.get_task("t-out"), len(router.calls), \
            await store.unread_count()
    task, calls, items = _run(scenario())
    require(not task.enabled, "nach genug Fehlschlaegen wird pausiert")
    require_equal(task.state, S.PAUSED, "sichtbar, nicht geloescht")
    require(calls <= MAX_FAILURES, f"nicht endlos versucht ({calls})")
    require_equal(items, 0, "und keine Meldung je Fehlversuch")
    require(task.last_error, "der Grund steht daneben")


def t_a_failed_run_actually_defers_the_next_attempt():
    """Der Rueckzug muss WIRKEN, nicht nur ausrechenbar sein.

    Mein erster Ausfalltest hat `retry_after` in jeder Runde selbst
    zurueckgesetzt, um schnell viele Fehlschlaege zu erzeugen — und hat damit
    genau das ueberdeckt, was er pruefen sollte. Eine Mutation, die den Rueckzug
    ersatzlos strich, kam durch.
    """
    async def scenario():
        store = _store()
        now = [time.time()]
        router = _Router({}, outcome=OUT.EXECUTOR_UNAVAILABLE,
                         reason="executor_unavailable")
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now[0])
        await store.put_task(_task(store, task_id="t-back", now=now[0],
                                   schedule=every(300).as_dict(),
                                   next_run_at=now[0] - 1))
        await scheduler.poll()
        await asyncio.sleep(0.3)
        after_first = await store.get_task("t-back")
        # Ohne die Uhr zu bewegen: die Aufgabe darf JETZT nicht wieder faellig
        # sein, obwohl ihr Termin in der Vergangenheit liegt.
        due_now = await store.due_tasks(now[0])
        second = await scheduler.poll()
        await asyncio.sleep(0.2)
        return after_first, due_now, second, len(router.calls)
    task, due_now, second, calls = _run(scenario())
    require_equal(task.consecutive_failures, 1, "der Fehlschlag ist gezaehlt")
    require(task.retry_after is not None, "und ein Rueckzug ist gesetzt")
    require(task.retry_after > task.last_run_at, "er liegt in der Zukunft")
    require_equal([t.task_id for t in due_now], [],
                  "waehrend des Rueckzugs gilt die Aufgabe nicht als faellig")
    require_equal(second, 0, "es wird also nicht sofort erneut versucht")
    require_equal(calls, 1, "und der Anbieter wird nicht gehaemmert")


def t_a_healthy_run_resets_the_backoff():
    async def scenario():
        store = _store()
        router = _Router({"termine": [{"id": "x"}]})
        now = time.time()
        scheduler = Scheduler(_Dispatcher(router), store, tick=0.01,
                              clock=lambda: now)
        await store.put_task(_task(store, task_id="t-heal", now=now,
                                   schedule=every(300).as_dict(),
                                   next_run_at=now - 1, consecutive_failures=4,
                                   last_error="frueher kaputt"))
        await scheduler.poll()
        await asyncio.sleep(0.3)
        return await store.get_task("t-heal")
    task = _run(scenario())
    require_equal(task.consecutive_failures, 0, "der Zaehler faellt zurueck")
    require_equal(task.last_error, "", "und der alte Fehler verschwindet")
    require(task.retry_after is None, "kein Rueckzug mehr")


# -- Posteingang --------------------------------------------------------------

def t_the_inbox_survives_a_new_store_object():
    async def scenario():
        path = os.path.join(tempfile.mkdtemp(), "p.sqlite3")
        store = S.ProactiveStore(path)
        await store.put_task(_task(store, task_id="t-p"))
        await store.add_item({"notification_id": "pn-1", "task_id": "t-p",
                              "run_id": "r", "priority": "normal",
                              "summary": "bleibt", "fingerprint": "f",
                              "content_trust": ""})
        reopened = S.ProactiveStore(path)
        return await reopened.unread(), await reopened.unread_count()
    items, count = _run(scenario())
    require_equal(count, 1, "die Meldung ueberlebt")
    require_equal(items[0]["zusammenfassung"], "bleibt", "mit Inhalt")


def t_reading_a_notice_removes_it_from_the_unread_list():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="t-r"))
        await store.add_item({"notification_id": "pn-9", "task_id": "t-r",
                              "run_id": "r", "priority": "normal",
                              "summary": "x", "fingerprint": "f",
                              "content_trust": ""})
        first = await store.mark_read("pn-9")
        second = await store.mark_read("pn-9")
        return first, second, await store.unread_count()
    first, second, count = _run(scenario())
    require(first, "beim ersten Mal gelesen")
    require(not second, "beim zweiten Mal war es schon gelesen")
    require_equal(count, 0, "und es taucht nicht mehr auf")


def t_important_notices_come_first():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="t-s"))
        for index, priority in enumerate(("normal", "wichtig", "normal")):
            await store.add_item({"notification_id": f"pn-{index}",
                                  "task_id": "t-s", "run_id": "r",
                                  "priority": priority, "summary": priority,
                                  "fingerprint": f"f{index}", "content_trust": ""})
        return await store.unread()
    items = _run(scenario())
    require_equal(items[0]["dringlichkeit"], "wichtig", "Wichtiges zuerst")


def t_the_inbox_is_pruned_so_it_does_not_become_a_dump():
    async def scenario():
        store = _store()
        await store.put_task(_task(store, task_id="t-prune"))
        for index in range(30):
            await store.add_item({"notification_id": f"pn-{index}",
                                  "task_id": "t-prune", "run_id": "r",
                                  "priority": "normal", "summary": "x",
                                  "fingerprint": f"f{index}", "content_trust": ""})
        removed = await store.prune(keep=10)
        return removed, await store.unread_count()
    removed, left = _run(scenario())
    require(removed >= 20, f"es wird wirklich aufgeraeumt ({removed})")
    require(left <= 10, f"und der Deckel haelt ({left})")


def t_the_delivery_promise_is_truthful():
    """Push is conditional on actual Apple setup, never inferred from inbox writes."""
    require("iPhone" in ZUSTELLUNG, "die Grenze wird benannt")
    require("benötigen einen eingerichteten Apple-Push-Zugang" in ZUSTELLUNG, "setup boundary must remain explicit")
    for source in ("scheduler.py", "runner.py", "store.py"):
        text = open(os.path.join(PROACTIVE_DIR, source), encoding="utf-8").read()
        for forbidden in ("apns", "push_notification", "send_push"):
            require(forbidden not in text.lower(), f"{forbidden} in {source}")


# -- Sprachweg ----------------------------------------------------------------

def t_a_new_conversation_learns_that_something_is_waiting():
    core = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                             "realtime", "core_server.py"), encoding="utf-8").read()
    require("_mention_proactive" in core, "der Hinweis existiert")
    body = core[core.index("async def _mention_proactive"):]
    body = body[:body.index("\n    def ")] if "\n    def " in body else body
    require("unread_count" in body, "die Anzahl wird gelesen")
    require('"role": "system"' in body,
            "als Systemhinweis, nicht als erfundener Nutzertext")
    require("limit=3" in body, "hoechstens drei Stichworte statt zwanzig Meldungen")
    require("proactive_list" in body, "Einzelheiten holt das Modell auf Nachfrage")


def t_the_scheduler_is_stopped_before_the_runtimes_it_uses():
    core = open(os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                             "realtime", "core_server.py"), encoding="utf-8").read()
    # Auf den Abschaltblock von serve() ankern, nicht auf das erste `finally:`
    # der Datei — davon gibt es mehrere, und tiefer eingerueckte enthalten die
    # gesuchte Zeichenkette als Teilstring.
    tail = core[core.index("await asyncio.Future()"):]
    require("scheduler.stop)" in tail, "der Scheduler wird angehalten")
    require("_stop_child_runtimes" in tail, "die Kindprozesse auch")
    require(tail.index("scheduler.stop)") < tail.index("_stop_child_runtimes"),
            "und zwar bevor der tiefe Ausfuehrende abgeraeumt wird")
    require(tail.index("scheduler.stop)") < tail.index("control.stop)"),
            "und vor dem Kontroll-Socket")


def t_durability_does_not_depend_on_a_clean_shutdown():
    """SIGKILL/Stromverlust bleiben moeglich, auch mit geordnetem SIGTERM.

    Deshalb darf nichts, was ueberleben muss, erst beim Herunterfahren
    geschrieben werden.
    """
    scheduler = open(os.path.join(PROACTIVE_DIR, "scheduler.py"),
                     encoding="utf-8").read()
    stop_body = scheduler[scheduler.index("    async def stop"):]
    stop_body = stop_body[:stop_body.index("    async def recover")]
    for forbidden in ("put_task", "add_item", "finish_run", "commit"):
        require(forbidden not in stop_body,
                f"{forbidden} beim Herunterfahren — das laeuft evtl. nie")


# -- Sprachformen --------------------------------------------------------------

def t_natural_language_becomes_a_typed_schedule_or_a_question():
    now = time.time()
    cases = {"in 20 Minuten": Kind.ONE_SHOT, "in 2 stunden": Kind.ONE_SHOT,
             "taeglich um 7:30": Kind.DAILY, "jeden Morgen": Kind.DAILY,
             "montags 08:00": Kind.WEEKLY, "alle 15 minuten": Kind.INTERVAL}
    for text, kind in cases.items():
        plan, _note = parse_when(text, now=now)
        require(plan is not None, f"{text!r} wurde nicht verstanden")
        require_equal(plan.kind, kind, f"{text!r} falsch eingeordnet")
    for text in ("irgendwann", "wenn du Zeit hast", ""):
        plan, _note = parse_when(text, now=now)
        require(plan is None, f"{text!r} haette nachgefragt werden muessen")


def t_spoken_numbers_are_understood_not_only_digits():
    """Ein Mensch sagt „in zwei Minuten", nicht „in 2 Minuten".

    Der erste Parser kannte nur Ziffern. Aufgefallen ist das erst im Live-Lauf —
    und zwar NACH einer erteilten Freigabe, weil der Einwand damals erst bei der
    Ausfuehrung kam.
    """
    now = time.time()
    for text in ("in zwei minuten", "in einer stunde", "alle fuenfzehn minuten",
                 "alle fünfzehn minuten", "taeglich um halb acht",
                 "jeden morgen um sieben uhr dreissig"):
        plan, _note = parse_when(text, now=now)
        require(plan is not None, f"{text!r} wurde nicht verstanden")
    plan, _ = parse_when("in zwei minuten", now=now)
    require(abs((plan.at_epoch - now) - 120) < 2, "zwei Minuten sind 120 Sekunden")
    plan, _ = parse_when("taeglich um halb acht", now=now)
    require_equal((plan.hour, plan.minute), (7, 30), "halb acht ist 7:30")
    plan, _ = parse_when("in 2 Minuten", now=now)
    require(plan is not None, "Ziffern gehen weiterhin")


def t_an_unusable_schedule_is_refused_before_anyone_is_asked():
    """Niemand soll etwas bestaetigen, das danach an einer Formulierung scheitert.

    Im Live-Lauf kam der Einwand erst NACH der Freigabe, und der eingefrorene
    Kontrollpfad musste ihn als `recovery_required` melden — er kann einen
    Einwand nicht von einem halb gelaufenen Schreibvorgang unterscheiden.
    Deshalb prueft jetzt der Beschreiber, den der Router VOR der Freigabe ruft.
    """
    from solvio.capabilities.proactive import describe_create

    async def described(arguments):
        try:
            return await describe_create(arguments)
        except CapabilityDeclined as exc:
            return exc

    good = _run(described({"titel": "x", "wann": "in zwei minuten",
                           "aktion": "calendar_list_events"}))
    require(isinstance(good, dict), f"ein gueltiger Auftrag geht durch: {good}")
    require_equal(good["art"], "one_shot", "und nennt die verstandene Art")
    require_equal(good["wann"], "in zwei minuten",
                  "in den Worten des Menschen, nicht als gerechneter Zeitpunkt")

    bad = _run(described({"titel": "x", "wann": "irgendwann",
                          "aktion": "calendar_list_events"}))
    require(isinstance(bad, CapabilityDeclined), "ein unverstaendlicher wird abgelehnt")
    require_equal(bad.reason, "schedule_not_understood", "mit klarem Grund")

    writing = _run(described({"titel": "x", "wann": "in zwei minuten",
                              "aktion": "gmail_send_draft"}))
    require(isinstance(writing, CapabilityDeclined),
            "und eine schreibende Aktion ebenfalls — vor der Freigabe")
    require_equal(writing.reason, "action_not_allowed", "mit klarem Grund")


def t_the_description_is_stable_over_time():
    """Sonst driftet die Freigabe zwischen Anfrage und Ausfuehrung.

    Der Digest wird aus der Beschreibung gebildet und bei der Fortsetzung erneut
    geprueft — er ist der Schutz davor, dass nach der Zustimmung etwas anderes
    laeuft. Mein erster Beschreiber setzte fuer „in zwei Minuten" den ABSOLUTEN
    Zeitpunkt ein. Der ist Sekunden spaeter ein anderer, und der echte Lauf
    endete mit `approval_drift`: der Schutz hat gegriffen, die Beschreibung war
    falsch gebaut.
    """
    from solvio.capabilities.proactive import describe_create

    async def twice(arguments):
        first = await describe_create(arguments, clock=lambda: 1_000_000.0)
        second = await describe_create(arguments, clock=lambda: 1_000_600.0)
        return first, second

    for wann in ("in zwei minuten", "taeglich um 7:30", "montags 08:00",
                 "alle 15 minuten"):
        first, second = _run(twice({"titel": "x", "wann": wann,
                                    "aktion": "calendar_list_events"}))
        require_equal(first, second,
                      f"{wann!r}: die Beschreibung aenderte sich mit der Uhr")
        for value in first.values():
            require(not isinstance(value, float),
                    f"{wann!r}: ein Gleitkommawert riecht nach Zeitstempel: {first}")

    # Und der Gegenbeweis: eine ANDERE Bitte beschreibt sich anders.
    a, _ = _run(twice({"titel": "x", "wann": "taeglich um 7:30",
                       "aktion": "calendar_list_events"}))
    b, _ = _run(twice({"titel": "x", "wann": "taeglich um 8:30",
                       "aktion": "calendar_list_events"}))
    require(a != b, "sonst waere der Digest blind fuer echte Aenderungen")


def t_the_describer_runs_before_the_approval_is_requested():
    """Strukturell: der Router ruft den Beschreiber vor `_mobile.request`."""
    router_source = open(os.path.join(os.path.dirname(__file__), "..", "src",
                                      "solvio", "capabilities", "router.py"),
                         encoding="utf-8").read()
    block = router_source[router_source.index("async def _mobile_approval"):]
    # Ende der Methode statt einer Zeile in ihrer Mitte. Der fruehere Anker
    # (`self._outstanding.pop(...)`) stand genau einmal im Text; seit der Router
    # eine bereits freigegebene Anfrage fortsetzen kann, steht er zweimal — und
    # der erste liegt VOR `_mobile.request`. Die Zusicherung ist unveraendert,
    # sie prueft jetzt nur ueber den ganzen Pfad statt ueber sein erstes Stueck.
    block = block[:block.index("    def _approval_failure(")]
    require("self._describe(" in block, "der Beschreiber laeuft in diesem Pfad")
    require(block.index("self._describe(") < block.index("_mobile.request"),
            "und zwar VOR der Anfrage an das iPhone")
    from solvio.capabilities.proactive import register as register_proactive
    import inspect as _inspect
    source = _inspect.getsource(register_proactive)
    require("describe=" in source, "background_create bekommt einen Beschreiber")


def t_there_is_no_free_form_cron_field():
    """Ein Feld, in das `* * * * *` passt, ist eine Einladung."""
    schema = SPECS["background_create"].input_schema["properties"]
    for forbidden in ("cron", "crontab", "expression", "interval_seconds",
                      "sekunden"):
        require(forbidden not in schema, f"{forbidden} ist frei setzbar")
    require("wann" in schema, "es gibt ein Feld fuer gewoehnliche Worte")
    code = _code_only(os.path.join(os.path.dirname(__file__), "..", "src",
                                   "solvio", "tools",
                                   "proactive_capability_tools.py")).lower()
    require("cron" not in code, "und auch die Bruecke kennt kein Cron")
    plan_code = _code_only(os.path.join(PROACTIVE_DIR, "schedule.py")).lower()
    require("cron" not in plan_code, "der Zeitplan selbst erst recht nicht")


def t_resource_limits_are_core_owned_not_model_chosen():
    from solvio.proactive.scheduler import MAX_TASKS_PER_OWNER, RUN_TIMEOUT
    require(MAX_CONCURRENT <= 4, "gleichzeitige Laeufe sind gedeckelt")
    require(0 < MAX_TASKS_PER_OWNER <= 100, "und die Zahl der Aufgaben auch")
    require(RUN_TIMEOUT <= 3600, "ein Lauf endet spaetestens nach einer Stunde")
    schema = SPECS["background_create"].input_schema["properties"]
    for forbidden in ("timeout", "max_runs", "concurrency", "budget", "prioritaet"):
        require(forbidden not in schema, f"{forbidden} waere vom Modell setzbar")



def t_jede_faehigkeit_hat_eine_werkzeugbeschreibung():
    """Sonst oeffnet gar keine Sprachsitzung mehr.

    Live gefunden, und teuer: eine neu registrierte Faehigkeit ohne Eintrag in
    der Werkzeugschicht liess `core.session_open_failed` mit einem KeyError
    auflaufen — beim Aufbau der Anbietersitzung, also BEVOR ein einziges Wort
    gesprochen war. Der Satellit verband sich, flog sofort wieder raus, und
    „Hey Solvio" blieb vier Mal unbeantwortet.

    Kein einziger Test hat das gefangen: alle riefen die Faehigkeiten direkt
    auf, keiner baute die Werkzeugliste so, wie der Sprachweg sie baut. Eine
    Registrierung ohne Beschreibung ist damit kein Schoenheitsfehler, sondern
    ein stummer Assistent.
    """
    import solvio.tools.proactive_capability_tools as T
    from solvio.capabilities.proactive import SPECS
    fehlt = sorted(set(SPECS) - set(T._SCHEMAS))
    require_equal(fehlt, [],
                  f"ohne Werkzeugbeschreibung, die Sitzung wuerde nicht oeffnen: {fehlt}")
    ueberzaehlig = sorted(set(T._SCHEMAS) - set(SPECS))
    require_equal(ueberzaehlig, [],
                  f"beschrieben, aber nicht registriert: {ueberzaehlig}")



if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
