"""Abo-Grenzen im echten Orchestrator, ausschliesslich temporaere Stores.

Gestellt sind Anbieter und Uhr. Keine CLI, kein Netz, keine produktive Ablage.
"""
from __future__ import annotations

import asyncio
import sqlite3
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_runtime_orchestrator as H
import test_agent_task_conversation as C
from solvio.agent_runtime import orchestrator as O, planner as PL, store as S
from solvio.agent_runtime import inquiry as Q, specialists as SP
from solvio.specialists.result import SpecialistResult


#: Der reale Wert `auth` je Grund (specialists/providers.py): eine per API-Schluessel angemeldete
#: CLI meldet `api_key` — Review Runde 16, R16-W1: die Attrappe stellte `subscription` ein und
#: verdeckte, dass der Providergrenzen-Datensatz mit `auth: api_key` verweigert wurde.
_AUTH_BY_REASON = {"logged_out": "none", "auth_unknown": "unknown", "subscription_required": "api_key"}


def _call(ok=False, reason="quota"):
    return PL.PlannerCall(ok, reason="" if ok else reason, provider="codex",
                          billing_mode="subscription", auth=_AUTH_BY_REASON.get(reason, "subscription"))


class Planner(H.FakePlanner):
    route = {"provider": "codex", "billing_mode": "subscription"}

    def __init__(self, reason="quota", steps=None, observed_billing="subscription"):
        super().__init__(steps=steps or [PL.PlannedStep(kind="capability", capability="ha_light_set")])
        self.reason = reason
        self.observed_billing = observed_billing

    async def plan(self, **kw):
        if self.reason:
            self.calls += 1
            kw["ledger"].check_planner()
            kw["ledger"].note_planner_call()
            call = _call(reason=self.reason)
            call.billing_mode = self.observed_billing
            raise PL.ProviderUnavailable(call)
        plan, _ = await super().plan(**kw)
        return plan, _call(True)


def _new(planner=None, scope="research"):
    planner = planner or Planner()
    orch = H._orch(planner=planner)
    with patch.object(orch, "_build_available", return_value=True):
        task, run = orch.create_task(objective="Untersuche den Auftrag und fuehre den Schritt aus",
            scope=scope, origin="local_owner", principal="local-owner")
    if scope == "build":
        orch.ledger.set_run_fields(run.run_id, workspace_path=tempfile.mkdtemp(prefix="solvio-wait-test-"))
    return orch, task, run


def _ticks(orch, count):
    for _ in range(count):
        H._run(orch.tick())


def t_subscription_blockers_keep_the_same_task_and_do_not_poll_or_switch():
    for reason in O.PROVIDER_BLOCKERS:
        orch, task, run = _new(Planner(reason))
        _ticks(orch, 2)
        state = orch.ledger.get_run(run.run_id)
        require_equal(state.state, S.WAITING_USER, reason)
        require_equal(state.planner_calls, 1, "die Zweierreservierung blieb stehen")
        require_equal(orch.ledger.get_task(task.task_id).state, S.TASK_ACTIVE)
        require_equal(state.finished_at, None)
        _ticks(orch, 3)
        require_equal(orch.planner.calls, 1)
        require_equal(orch.router.calls, [])


def t_owner_resume_books_only_real_wait_once_and_keeps_consumed_work():
    with patch("time.time", return_value=1000.0):
        orch, task, run = _new()
        _ticks(orch, 2)
    planner = Planner("")
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner, router=orch.router)
    with patch("time.time", return_value=8000.0):
        H._run(after.reconcile())
        _ticks(after, 2)
        require_equal(planner.calls, 0, "Neustart setzte ohne Owner fort")
        require(H._run(after.resume(run.run_id)))
        require(not H._run(after.resume(run.run_id)), "doppelte Wiederaufnahme")
        current = after.ledger.get_run(run.run_id)
        require_equal(current.started_at, 1000.0)
        require_equal(current.provider_wait_seconds, 7000.0)
        require_equal(current.planner_calls, 1)
        _ticks(after, 2)
        require_equal(after.ledger.get_run(run.run_id).planner_calls, 2)
        require_equal(len(after.router.calls), 1, "wartender Auftrag wurde nicht fortgesetzt")
        require_equal(len(after.ledger.runs_for_task(task.task_id)), 1)


def t_two_owner_resumes_credit_the_interval_only_once():
    async def go():
        with patch("time.time", return_value=1000.0):
            orch, _, run = _new()
            await orch.tick()
            await orch.tick()
        second = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=orch.planner)
        with patch("time.time", return_value=2000.0):
            results = await asyncio.gather(orch.resume(run.run_id), second.resume(run.run_id))
        require_equal(sorted(results), [False, True])
        require_equal(orch.ledger.get_run(run.run_id).provider_wait_seconds, 1000.0)
    H._run(go())


def t_route_changes_do_not_move_an_open_task_to_another_provider():
    orch, _, run = _new()
    _ticks(orch, 2)
    other = Planner("")
    other.route = {"provider": "claude-code", "billing_mode": "subscription"}
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=other)
    require(not H._run(after.resume(run.run_id)))
    _ticks(after, 2)
    require_equal(other.calls, 0)
    require_equal(after.ledger.get_run(run.run_id).state, S.WAITING_USER)


def t_subscription_auth_gate_failures_park_and_resume_after_login_is_fixed():
    for reason, billing in (("logged_out", "unknown"), ("auth_unknown", "unknown"),
                             ("subscription_required", "metered_api")):
        planner = Planner(reason, observed_billing=billing)
        orch, _, run = _new(planner)
        _ticks(orch, 2)
        view = Q.run_view(orch.ledger, orch.ledger.get_run(run.run_id))
        require_equal(view["zustand_code"], S.WAITING_USER, f"{reason}: {view.get('fehler_code') or view.get('failure_category', '')}")
        boundary = json.loads(orch.ledger.get_run(run.run_id).boundary or "{}")
        require_equal(boundary.get("provider_wait", {}).get("auth"), _AUTH_BY_REASON[reason],
                      "the boundary record lost the real auth value")
        require_equal(view["abrechnung"], billing)
        require_equal(view["angeforderte_abrechnung"], "subscription")
        require_equal(view["anbieter_aufgerufen"], False)
        planner.reason = ""
        require(H._run(orch.resume(run.run_id)), "behobene Anmeldung blieb unfortsetzbar")
        _ticks(orch, 2)
        require_equal(len(orch.router.calls), 1)


def t_replan_resume_after_two_restarts_does_not_replay_an_earlier_effect():
    steps = [PL.PlannedStep(kind="capability", capability="ha_light_set"),
             PL.PlannedStep(kind="specialist", profile="investigator/codex", instruction="Pruefe X")]
    planner = Planner("", steps)
    orch, _, run = _new(planner, scope="build")
    _ticks(orch, 3)
    planner.reason = "quota"
    with patch.object(SP, "run_specialist", H._fake_specialist(ok=False)):
        _ticks(orch, 1)
    require_equal(orch.ledger.get_run(run.run_id).state, S.WAITING_USER)
    planner.reason = ""
    planner._steps = [PL.PlannedStep(kind="capability", capability="calendar_create_event")]
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner, router=orch.router)
    H._run(after.reconcile())
    require(H._run(after.resume(run.run_id)))
    # Das Fenster direkt nach dem Owner-Resume ist eine eigene Grenze.
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner, router=orch.router)
    H._run(after.reconcile())
    with patch.object(after, "_checkpoint_if_open", side_effect=_Crash()):
        try:
            _ticks(after, 1)
        except _Crash:
            pass
    # Jetzt ist der Ersatzplan berechnet und der Resume-Marker geloescht,
    # aber der gewoehnliche Takt-Checkpoint ist nie erreicht worden.
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner, router=orch.router)
    H._run(after.reconcile())
    _ticks(after, 1)
    require_equal([c["name"] for c in after.router.calls], ["ha_light_set", "calendar_create_event"])


def t_real_work_time_and_attempt_counts_are_not_reset_by_waiting():
    orch, _, run = _new()
    _ticks(orch, 1)
    start = orch._contexts[run.run_id].ledger.started_at
    with patch("time.time", return_value=start + 200.0):
        _ticks(orch, 1)
    with patch("time.time", return_value=start + 9000.0):
        require(H._run(orch.resume(run.run_id)))
        current = orch.ledger.get_run(run.run_id)
        limits = orch._contexts[run.run_id].ledger
        require_equal(current.provider_wait_seconds, 8800.0)
        require_equal(limits.remaining_seconds(), limits.budget.seconds - 200.0)
        require_equal(current.planner_calls, 1)
        require_equal(limits.planner_calls, 1)


def t_inquiry_exposes_the_wait_and_real_billing_without_claiming_success():
    orch, _, run = _new()
    _ticks(orch, 2)
    view = Q.run_view(orch.ledger, orch.ledger.get_run(run.run_id))
    require_equal(view["zustand_code"], S.WAITING_USER)
    require_equal(view["anbieter"], "codex")
    require_equal(view["abrechnung"], "subscription")
    require_equal(view["anbietergrenze"]["grund"], "quota")
    require_equal(view["anbieternutzung_gemeldet"], False)
    require(view["wartet_auf"]["handlung"])
    require_equal(view["beendet"], None)


def _specialist_failure(dispatch_started):
    async def call(request, **kwargs):
        return SP.SpecialistRun(result=SpecialistResult(role="builder", provider="codex",
            question=request.objective, ok=False, reason="quota"), quota=True,
            provider="codex", billing_mode="subscription", auth="subscription",
            dispatch_started=dispatch_started)
    return call


def t_readonly_specialist_wait_resumes_without_repeating_an_earlier_effect():
    steps = [PL.PlannedStep(kind="capability", capability="ha_light_set"),
             PL.PlannedStep(kind="specialist", profile="investigator/codex", instruction="Pruefe X")]
    orch, _, run = _new(Planner("", steps), scope="build")
    _ticks(orch, 3)
    with patch.object(SP, "run_specialist", _specialist_failure(True)):
        _ticks(orch, 1)
    require_equal(orch.ledger.get_run(run.run_id).state, S.WAITING_USER)
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=orch.planner, router=orch.router)
    H._run(after.reconcile())
    require(H._run(after.resume(run.run_id)))
    with patch.object(SP, "run_specialist", H._fake_specialist()):
        _ticks(after, 1)
    require_equal(len(after.router.calls), 1, "erste Wirkung wiederholt")
    specialist = [s for s in after.ledger.steps_for_run(run.run_id) if s.kind == "specialist"]
    require_equal(len(specialist), 1, "wartenden Schritt nicht wiederaufgenommen")
    require_equal(specialist[0].state, "succeeded")


def t_builder_partial_work_stays_parked_instead_of_repeating():
    orch, _, run = _new(Planner(""), scope="build")
    _ticks(orch, 2)
    context = orch._contexts[run.run_id]
    planned = PL.PlannedStep(kind="specialist", profile="builder/codex", instruction="Bearbeite X")
    with patch.object(SP, "run_specialist", _specialist_failure(True)):
        H._run(orch._run_specialist_step(orch.ledger.get_run(run.run_id), context, planned, 1))
    current = orch.ledger.get_run(run.run_id)
    require_equal(current.state, S.WAITING_USER)
    require(not H._run(orch.resume(run.run_id)), "Builder-Teilwirkung erneut gestartet")
    require_equal(Q.run_view(orch.ledger, current)["anbietergrenze"]["fortsetzbar"], False)


def t_builder_login_rejected_before_dispatch_can_resume_safely():
    steps = [PL.PlannedStep(kind="specialist", profile="builder/codex", instruction="Bearbeite X")]
    orch, _, run = _new(Planner("", steps), scope="build")
    _ticks(orch, 2)
    async def logged_out(request, **kwargs):
        outcome = await _specialist_failure(False)(request)
        outcome.result.reason = "logged_out"
        outcome.quota = False
        outcome.billing_mode = "unknown"
        outcome.auth = "none"
        return outcome
    with patch.object(SP, "run_specialist", logged_out):
        _ticks(orch, 1)
    require_equal(orch.ledger.get_run(run.run_id).state, S.WAITING_USER)
    require(H._run(orch.resume(run.run_id)))
    with patch.object(SP, "run_specialist", H._fake_specialist()):
        _ticks(orch, 1)
    require_equal(orch.ledger.steps_for_run(run.run_id)[0].state, "succeeded")


def t_assessment_wait_keeps_the_result_and_resumes_the_real_verification():
    class Assessor(C.Planer):
        route = {"provider": "codex", "billing_mode": "subscription"}
        blocked = True

        async def assess(self, **kw):
            if self.blocked:
                self.assess_calls += 1
                return _call()
            return await super().assess(**kw)

    planner = Assessor([[C.SCOUT]], anforderungen=C.ANFORDERUNGEN, urteile=[C.URTEIL_FERTIG])
    researcher = C.Rechercheur(antworten=(C.ANTWORT_2,))
    orch = H._orch(planner=planner, researcher=researcher, fixture_file=C.__file__)
    _, run = orch.create_task(objective=C.ZIEL_RECHERCHE, scope="research",
                             origin="local_owner", principal="local-owner")
    _ticks(orch, 5)
    require_equal(orch.ledger.get_run(run.run_id).state, S.WAITING_USER)
    require_equal(len(researcher.calls), 1)
    planner.blocked = False
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner, researcher=researcher, fixture_file=C.__file__)
    H._run(after.reconcile())
    require(H._run(after.resume(run.run_id)))
    # Auch ein weiterer Prozessverlust direkt nach Resume behaelt VERIFYING.
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner, researcher=researcher, fixture_file=C.__file__)
    H._run(after.reconcile())
    _ticks(after, 1)
    require_equal(len(researcher.calls), 1, "die Recherche wurde erneut gestartet")
    require_equal(after.ledger.get_run(run.run_id).assessment_calls, 2)
    require_equal(after.ledger.get_run(run.run_id).state, S.SUCCEEDED)


class _Crash(BaseException):
    pass


def t_mid_assessment_is_retried_before_any_following_effect_even_after_crash():
    for reopen in (False, True):
        for finished in (False, True):
            class Assessor(C.Planer):
                route = {"provider": "codex", "billing_mode": "subscription"}
                blocked = True

                async def assess(self, **kw):
                    if self.blocked:
                        self.assess_calls += 1
                        return _call()
                    return await super().assess(**kw)

            effect = PL.PlannedStep(kind="capability", capability="ha_light_set")
            planner = Assessor([[C.SCOUT, effect]], anforderungen=C.ANFORDERUNGEN,
                               urteile=[C.URTEIL_FERTIG if finished else C.URTEIL_WEITER])
            researcher = C.Rechercheur(antworten=(C.ANTWORT_2,))
            orch = H._orch(planner=planner, researcher=researcher, fixture_file=C.__file__)
            _, run = orch.create_task(objective=C.ZIEL_RECHERCHE, scope="research",
                                     origin="local_owner", principal="local-owner")
            _ticks(orch, 2)
            if reopen:
                # Der Park ist schon festgeschrieben, der uebliche Takt-
                # Checkpoint danach kommt jedoch NIE. Genau diese Luecke war offen.
                with patch.object(orch, "_checkpoint_if_open", side_effect=_Crash()):
                    try:
                        _ticks(orch, 1)
                    except _Crash:
                        pass
            else:
                _ticks(orch, 1)
            require_equal(orch.ledger.get_run(run.run_id).state, S.WAITING_USER)
            require_equal(orch.router.calls, [])
            if reopen:
                orch = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner,
                               researcher=researcher, router=orch.router, fixture_file=C.__file__)
                H._run(orch.reconcile())
            planner.blocked = False
            require(H._run(orch.resume(run.run_id)))
            if reopen:
                orch = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=planner,
                               researcher=researcher, router=orch.router, fixture_file=C.__file__)
                H._run(orch.reconcile())
            _ticks(orch, 1)
            require_equal(planner.assess_calls, 2, "Zwischenurteil wurde uebersprungen")
            require_equal(orch.router.calls, [], "Effekt kam vor dem Zwischenurteil")
            _ticks(orch, 2)
            require_equal(len(orch.router.calls), 0 if finished else 1)
            require_equal(len(researcher.calls), 1)
            if finished:
                require_equal(orch.ledger.get_run(run.run_id).state, S.SUCCEEDED,
                              "Ergebnis/Quellen gingen zwischen Park und Checkpoint verloren")


def t_crash_before_provider_park_does_not_replay_specialist_or_start_next_effect():
    steps = [PL.PlannedStep(kind="specialist", profile="investigator/codex", instruction="Pruefe X"),
             PL.PlannedStep(kind="capability", capability="ha_light_set")]
    orch, _, run = _new(Planner("", steps), scope="build")
    _ticks(orch, 2)
    calls = []
    async def failure(request, **kw):
        calls.append(request)
        return await _specialist_failure(False)(request)
    with patch.object(SP, "run_specialist", failure), \
            patch.object(orch.ledger, "park_provider_boundary", side_effect=_Crash()):
        try:
            _ticks(orch, 1)
        except _Crash:
            pass
    current = orch.ledger.get_run(run.run_id)
    require_equal(current.state, S.WAITING_SPECIALIST)
    require_equal(orch.ledger.steps_for_run(run.run_id)[0].state, "running",
                  "vor dem atomaren Park lag bereits ein retrybarer Schritt im Buch")
    after = H._orch(ledger=S.AgentRunLedger(orch.ledger.path), planner=orch.planner, router=orch.router)
    H._run(after.reconcile())
    with patch.object(SP, "run_specialist", failure):
        _ticks(after, 3)
    require_equal(len(calls), 1)
    require_equal(after.router.calls, [])
    require_equal(after.ledger.get_run(run.run_id).failure_category, "recovery_required")


def t_crash_between_step_and_run_updates_rolls_the_entire_provider_park_back():
    steps = [PL.PlannedStep(kind="specialist", profile="investigator/codex", instruction="Pruefe X")]
    orch, _, run = _new(Planner("", steps), scope="build")
    _ticks(orch, 2)
    checkpoint = orch.ledger.get_run(run.run_id).plan_checkpoint
    connect = orch.ledger._connect

    class Connection:
        def __init__(self):
            self.inner = connect()

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def execute(self, sql, *args):
            if "specialist_count=specialist_count+" in sql:
                # Der Schritt ist INNERHALB dieser Transaktion schon waiting.
                state = self.inner.execute("SELECT state FROM agent_steps WHERE run_id=?",
                                           (run.run_id,)).fetchone()[0]
                require_equal(state, "waiting")
                raise _Crash()
            return self.inner.execute(sql, *args)

    with patch.object(SP, "run_specialist", _specialist_failure(False)), \
            patch.object(orch.ledger, "_connect", Connection):
        try:
            _ticks(orch, 1)
        except _Crash:
            pass
    reopened = S.AgentRunLedger(orch.ledger.path)
    current = reopened.get_run(run.run_id)
    require_equal(current.state, S.WAITING_SPECIALIST)
    require_equal(current.boundary, "")
    require_equal(current.specialist_count, 0)
    require_equal(current.plan_checkpoint, checkpoint)
    require_equal(reopened.steps_for_run(run.run_id)[0].state, "running",
                  "der halbe Park wurde trotz Crash festgeschrieben")

def t_a_failure_after_the_park_stands_keeps_the_owner_boundary():
    """Review Runde 17, F17-1: die Runde-16-Korrektur umschloss die GANZE Grenzoeffnung —
    schrieb der Park bereits WAITING_USER und scheiterte erst die Ereigniszeile danach,
    endete der Lauf FAILED mit der Behauptung, die Grenze sei nicht gespeichert, und die
    Owner-Entscheidung (Wiederaufnahme) war weg. Jetzt: steht die Grenze, bleibt sie."""
    planner = Planner("subscription_required", observed_billing="metered_api")
    orch, _, run = _new(planner)
    real_event = orch.ledger.record_event
    def flaky(run_id, kind, *args, **kwargs):
        if kind == "boundary_opened":
            raise sqlite3.OperationalError("database is locked")
        return real_event(run_id, kind, *args, **kwargs)
    with patch.object(orch.ledger, "record_event", flaky):
        for _ in range(3):
            H._run(orch.tick())  # must not raise
    parked = orch.ledger.get_run(run.run_id)
    require_equal((parked.state, parked.failure_category or ""), (S.WAITING_USER, ""), parked.result_summary)
    require(bool(parked.boundary), "the boundary record is gone")
    require_equal(json.loads(parked.boundary).get("provider_wait", {}).get("auth"), "api_key")
    require_equal(planner.calls, 1)
    # and the owner can still resume it
    require(H._run(orch.resume(run.run_id)))


def t_a_standing_boundary_also_survives_a_failing_notice_not_just_a_failing_event_line():
    """Testluecke aus Review Runde 22 (H-1): gedeckt war bisher nur der Fehlschlag der
    EREIGNISZEILE — den faengt der Schutz IN `_open_provider_boundary` ab. Der aeussere
    Waechter im Planungszweig (`current.state == WAITING_USER and current.boundary` →
    nur loggen) blieb ungetestet: `if False:` liess die Suite gruen. Scheitert der HINWEIS
    an den Owner, steht der Park schon — und ein FAILED hier haette die Owner-Entscheidung
    ueberschrieben. Der Hinweis ist Zustellung, nie Autoritaet."""
    planner = Planner("subscription_required", observed_billing="metered_api")
    orch, _, run = _new(planner)

    async def failing_notice(*args, **kwargs):
        raise RuntimeError("notice transport down")

    with patch.object(O.notices, "send", failing_notice):
        for _ in range(3):
            H._run(orch.tick())  # must not raise
    parked = orch.ledger.get_run(run.run_id)
    require_equal((parked.state, parked.failure_category or ""), (S.WAITING_USER, ""), parked.result_summary)
    require(bool(parked.boundary), "the boundary record is gone after a failing notice")
    require_equal(json.loads(parked.boundary).get("provider_wait", {}).get("auth"), "api_key")
    require_equal(planner.calls, 1, "the tick must not re-plan behind a standing boundary")
    require(H._run(orch.resume(run.run_id)), "the owner decision must survive")


def t_a_failure_after_the_park_stands_keeps_the_owner_boundary_on_the_specialist_path_too():
    """Review Runde 18, F18-1: der Runde-17-Schutz sass nur im Planungspfad; ein Park aus dem
    Spezialistenpfad (WAITING_USER + Grenze bereits geschrieben) wurde durch die scheiternde
    Ereigniszeile danach FAILED (`step_failed`) — nicht wiederaufnehmbar. Jetzt sitzt der Schutz
    dort, wo der Park geschrieben wird, fuer jeden Aufrufer; der Owner-Hinweis geht trotzdem hinaus."""
    steps = [PL.PlannedStep(kind="specialist", profile="investigator/codex", instruction="Pruefe X")]
    orch, _, run = _new(Planner("", steps), scope="build")
    _ticks(orch, 2)
    real_event = orch.ledger.record_event
    def flaky(run_id, kind, *args, **kwargs):
        if kind == "boundary_opened":
            raise sqlite3.OperationalError("database is locked")
        return real_event(run_id, kind, *args, **kwargs)
    with patch.object(SP, "run_specialist", _specialist_failure(True)), \
            patch.object(orch.ledger, "record_event", flaky):
        for _ in range(3):
            H._run(orch.tick())  # must not raise, must not end the run
    parked = orch.ledger.get_run(run.run_id)
    require_equal((parked.state, parked.failure_category or ""), (S.WAITING_USER, ""), parked.result_summary)
    require(bool(parked.boundary), "the boundary record is gone")
    specialist = [st for st in orch.ledger.steps_for_run(run.run_id) if st.kind == "specialist"]
    require_equal((len(specialist), specialist[0].state), (1, "waiting"))
    require(H._run(orch.resume(run.run_id)), "the owner could not resume the parked run")


def t_a_boundary_write_that_fails_ends_the_run_honestly_and_never_leaves_the_tick():
    """Review Runde 16, R16-W1 (zweite Haelfte): eine Ausnahme aus dem Speichern der
    Anbietergrenze verliess den Takt — der Lauf blieb PLANNING, zaehlte je Takt eine
    Planer-Absicht und endete nach sieben Takten `budget_exhausted` (falsche Wahrheit);
    die nach ihm eingereihten Laeufe wurden in diesem Takt nicht bedient. Jetzt endet der
    Lauf ehrlich (`capability_failed`, Grund im Text), und der Takt kehrt zurueck."""
    from solvio.secret_vault.firewall import CredentialRefused
    planner = Planner("subscription_required", observed_billing="metered_api")
    orch, _, run = _new(planner)
    calls = {"n": 0}
    real = orch.ledger.park_provider_boundary
    def broken(*args, **kwargs):
        calls["n"] += 1
        raise CredentialRefused("credential_named_with_value", "agent_run.boundary")
    with patch.object(orch.ledger, "park_provider_boundary", broken):
        for _ in range(3):
            H._run(orch.tick())  # must not raise
    finished = orch.ledger.get_run(run.run_id)
    require_equal((finished.state, finished.failure_category), (S.FAILED, "capability_failed"))
    require("Anbietergrenze" in (finished.result_summary or "") and "CredentialRefused" in (finished.result_summary or ""),
            finished.result_summary)
    require_equal(calls["n"], 1, "the boundary was written more than once")
    require_equal(planner.calls, 1, "the planner was called again after the failed boundary")
    del real


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
