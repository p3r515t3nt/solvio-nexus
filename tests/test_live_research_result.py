"""A real admitted public voice task returns its result to the same live session.

Only provider events/selection and task execution are synthetic. The browser
proof, admission, ledger, transcript and result delivery are actual Core code.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_session as H
from solvio.agent_runtime import store as S
from solvio.agent_runtime.voice_delegate import LiveSelection
from solvio.realtime import live_session as L

QUESTION = 'Recherchiere das aktuelle Wetter in Dietzenbach und nenne eine Quelle.'
WEATHER = 'In Dietzenbach sind es 23 Grad; am Abend ist Regen möglich.'


def output(w):
    return '\n'.join(e['content'] for e in w.providers[0].sent
                     if e['type'] == 'session.commentary.append')


@asynccontextmanager
async def live_world():
    async def issued(w, client=None, headers=None):
        chat, _ = w.server.conversations.create_conversation(owner_principal='local-owner',
            client_request_id='public-weather-chat')
        response = await (client or w.client).post(H.H.V.SESSION_PATH,
            json={'conversation_id': chat['conversation_id']}, headers=headers or w.headers)
        require_equal(response.status, 201)
        return await response.json()
    with patch.object(H.H, 'issued', issued):
        async with H.world() as w:
            w.orch.conversations = w.server.conversations
            yield w


@asynccontextmanager
async def world():
    async with live_world() as w:
        w.selector.selection = LiveSelection(calls=({'name': 'agent_task_research',
            'arguments': {'objective': QUESTION}},))
        # Constants are patched only when implemented; the pre-fix regression
        # still reaches real task admission and proves the missing delivery.
        with patch.object(L, 'RESEARCH_POLL_SECONDS', .01, create=True), \
                patch.object(L, 'RESEARCH_WAIT_SECONDS', 2., create=True):
            w.ws, _ = await H.H.started(w)
            await w.providers[0].events.put(H.transcript(QUESTION))
            await w.providers[0].events.put(H.delegate())
            await H.until(lambda: len(w.ledger.recent_runs()) == 1)
            await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 2)
            w.run = w.ledger.recent_runs()[0]
            try:
                yield w
            finally:
                if not w.providers[0].closed:
                    await H.end(w, w.ws)


def finish(w, state=S.SUCCEEDED, summary=WEATHER):
    w.ledger.transition(w.run.run_id, S.PLANNING)
    w.ledger.transition(w.run.run_id, S.RUNNING)
    w.ledger.transition(w.run.run_id, state, result_summary=summary,
                        failure_category='specialist_failed' if state == S.FAILED else '')


async def t_completed_research_reaches_open_voice_without_another_model_call():
    async with world() as w:
        finish(w)
        await H.until(lambda: WEATHER in output(w), seconds=.4)
        require_equal(len(w.selector.calls), 1)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(output(w).count(WEATHER), 1)


async def t_failed_research_is_spoken_as_failure_not_success():
    async with world() as w:
        finish(w, S.FAILED, 'Die Wetterquelle war nicht erreichbar.')
        await H.until(lambda: 'Die Wetterquelle war nicht erreichbar.' in output(w), seconds=.4)
        require('fehlgeschlagen' in output(w).lower())
        require(WEATHER not in output(w))


async def t_closed_session_does_not_receive_late_research_result():
    async with world() as w:
        await H.end(w, w.ws)
        before = len(w.providers[0].sent)
        finish(w)
        await asyncio.sleep(.05)
        require_equal(len(w.providers[0].sent), before)
        require_equal(w.ledger.get_run(w.run.run_id).state, S.SUCCEEDED)


async def t_revoked_browser_proof_drops_waiting_result():
    async with world() as w:
        await w.service.revoke(w.auth['session_id'], principal='local-owner')
        finish(w)
        await H.until(lambda: w.providers[0].closed)
        require(WEATHER not in output(w))
        require_equal(w.sessions[0]._research_watchers, {})


async def t_user_boundary_reports_the_actual_required_action():
    from solvio.agent_runtime.boundaries import UserBoundary, PRODUCT_DECISION
    async with world() as w:
        action = 'Entscheide, ob die Recherche morgen weitergehen soll'
        boundary = UserBoundary(PRODUCT_DECISION, action, 'Das Kontingent ist erschöpft')
        w.ledger.set_run_fields(w.run.run_id, boundary=boundary.as_dict())
        finish(w, S.WAITING_USER, 'Die Recherche wartet auf dich.')
        await H.until(lambda: action in output(w), seconds=.4)
        require('wartet auf dich' in output(w))
        require_equal(w.ledger.get_run(w.run.run_id).state, S.WAITING_USER)
        require_equal(len(w.selector.calls), 1)


async def t_wrong_conversation_owner_or_private_scope_never_speaks_result():
    for field, value in (('conversation_ref', 'c-0123456789abcdef'),
                         ('created_principal', 'foreign-owner'), ('scope', 'task')):
        async with world() as w:
            with w.ledger._open() as db:
                db.execute('UPDATE agent_tasks SET ' + field + '=? WHERE task_id=?', (value, w.run.task_id))
            finish(w)
            await asyncio.sleep(.05)
            require(WEATHER not in output(w), field)


async def t_wait_is_bounded_and_never_claims_completion():
    async with live_world() as w:
        w.selector.selection = LiveSelection(calls=({'name': 'agent_task_research',
            'arguments': {'objective': QUESTION}},))
        with patch.object(L, 'RESEARCH_POLL_SECONDS', .01, create=True), \
                patch.object(L, 'RESEARCH_WAIT_SECONDS', .14, create=True):
            w.ws, _ = await H.H.started(w)
            await w.providers[0].events.put(H.transcript(QUESTION))
            await w.providers[0].events.put(H.delegate())
            await H.until(lambda: len(w.ledger.recent_runs()) == 1)
            w.server.idle_timeout = .07
            await H.until(lambda: 'Recherche läuft im Hintergrund weiter' in output(w), seconds=.5)
            require(w.sessions[0].active)
            require_equal(w.ledger.recent_runs()[0].state, S.CREATED)
            require(WEATHER not in output(w))
            closed = await H.H.receive(w.ws, 'session_closed')
            require_equal(closed['provider'], 'closed_confirmed')
            await H.H.closed(w.ws)
            rows=w.server.conversations.recent_context(w.sessions[0].conversation_id)
            require(any(r['role']=='assistant' and 'Sprachgespräch' in r['text']
                        and 'Hintergrund' in r['text'] for r in rows),
                    'Background handoff must remain readable even without a provider transcript')
            require('Sprachgespräch' in output(w) and 'Hintergrund' in output(w))
            require_equal(w.ledger.recent_runs()[0].state, S.CREATED)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
