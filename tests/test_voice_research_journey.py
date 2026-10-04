"""One offline journey through public voice, clarification, native research and chat.

Only provider protocols and public offer data are synthetic. Real WebSockets,
local native subprocesses, Hermes transport, task/cost/answer stores, completion
assessment and persisted results are used. This never proves live flight prices.
"""
import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_voice_research_answer as V
import test_hermes_native as N
from test_agent_cost_runtime import _cli
from solvio.agent_runtime import planner as PL, specialists as SP, store as S, cost_dispatch as D
from solvio.specialists import subscription as U, launcher as L
from solvio.tools.task_answer import TaskAnswerTool
from solvio.realtime import live_session as LIVE

GOAL = 'Suche einen günstigen Hin- und Rückflug nach Warschau, 12. bis 16. Oktober 2026, eine Person.'
OFFER = 'Synthetischer Flugvergleich: FRA–WAW, 12.–16.10.2026, eine Person, Hin- und Rückflug 120 Euro.'
CAVEAT = 'Testdaten; Aufgabegepäck ist nicht enthalten. Keine Aussage über aktuelle Buchbarkeit.'
SOURCE = 'https://example.org/source'
ANSWER = dict(findings=[OFFER], evidence=[SOURCE], assumptions=[], uncertainties=[],
    risk_notes=[], rejected_alternatives=[], confidence='hoch', recommended_path=OFFER + ' ' + CAVEAT)
CRITERIA = {'auskunft': [{'id': 'a1', 'text': GOAL}], 'handlungen': [], 'unklar': [],
    'belege': {'mindestens': 1}}
PLAN = {'schritte': [{'art': 'specialist', 'profil': 'researcher/hermes',
    'auftrag': 'Prüfe Datum, Strecke, Gesamtpreis und Bedingungen.'}], 'anforderungen': CRITERIA}
VERDICT = {'beantwortet': [{'id': 'a1', 'belege': [SOURCE]}], 'offen': [], 'fehlend': [],
    'unsicher': [], 'weiterarbeit_noetig': False}


async def drive(w, run_id, target):
    for _ in range(20):
        run = w.ledger.get_run(run_id)
        if run.state == target:
            return run
        require(run.state not in S.TERMINAL_STATES, (run.state, run.failure_category))
        await w.orch._advance(run)
        w.orch._checkpoint_if_open(run_id)
    raise AssertionError('journey did not reach ' + target)


async def journey(*, close_before_result):
    with N.native_fixture(timeout=5) as (root, config, _, _, _, _):
        (root / 'answer.json').write_text(json.dumps(ANSWER, ensure_ascii=False))
        with patch.object(V.C, 'SCRIPT', V.SCRIPT), patch.object(LIVE, 'RESEARCH_POLL_SECONDS', .01):
            async with V.C.world() as w:
                w.server.dispatcher.register(TaskAnswerTool(w.server.dispatcher))
                local_cli = _cli(root, w.ledger.path,
                    plans=[dict(PLAN, schritte=[], rueckfrage=V.QUESTION), PLAN], assessments=[VERDICT])
                previous_invocation = U.text_invocation
                def invocation(provider, *, workdir, **kwargs):
                    scope = D.current_scope()
                    if getattr(scope, 'phase', '') == 'voice_delegate':
                        return previous_invocation(provider, workdir=workdir, **kwargs)
                    return L.Invocation(str(local_cli), ('--json',), cwd=workdir, timeout=5)
                w.orch.planner = PL.Planner(subscription_transport=U.SubscriptionTransport('codex', timeout=5))
                w.orch.cost_quote_adapter = lambda *_: N.FREE
                w.orch.researcher = None
                with patch.object(U, 'text_invocation', invocation), \
                        patch.object(SP, 'native_research_configured', return_value=True), \
                        patch.object(SP, 'native_research_config', return_value=config), \
                        patch.object(SP, 'selected_research_profile', return_value='researcher/hermes'):
                    await V.C.say(w, GOAL, 'journey-start')
                    accepted = w.session._tool_results[-1]
                    require(accepted['success'], accepted)
                    run_id, task_id = accepted['data']['run_id'], accepted['data']['task_id']
                    grant = w.orch.task_authority.for_run(run_id)
                    await drive(w, run_id, S.WAITING_USER)
                    def output():
                        return '\n'.join(e['content'] for e in w.provider.sent if e['type'] == 'session.commentary.append')
                    try:
                        await V.C.C.H.until(lambda: V.QUESTION in output())
                    except AssertionError as exc:
                        raise AssertionError('question output missing: ' + output()) from exc
                    await V.C.say(w, V.ANSWER, 'journey-answer', 2000)
                    require_equal(w.session._tool_results[-1]['data']['run_id'], run_id)
                    require(V.ANSWER in V.Q.context(w.ledger, run_id))
                    require_equal(len(w.native_launches), 3)
                    if close_before_result:
                        await V.C.C.H.end(w, w.client)
                    final = await drive(w, run_id, S.SUCCEEDED)
                    require_equal(w.orch.task_authority.for_run(run_id), grant)
                    require_equal(len(w.ledger.runs_for_task(task_id)), 1)
                    require_equal([c['phase'] for c in D.invocations(w.ledger, task_id)],
                        ['plan', 'plan', 'specialist', 'assessment'])
                    require_equal(len(N.method_rows(root, 'turn/start')), 1)
                    native_prompt = json.dumps(N.method_rows(root, 'turn/start'), ensure_ascii=False)
                    require(GOAL in native_prompt and V.ANSWER in native_prompt)
                    if not close_before_result:
                        await V.C.C.H.until(lambda: OFFER in output())
                        await V.C.C.H.end(w, w.client)
                    # Reopen the actual store; no remembered selection or global last task.
                    reopened = S.AgentRunLedger(w.ledger.path)
                    view = V.inquiry.run_view(reopened, reopened.get_run(run_id))
                    require_equal((view['zustand_code'], view['auftrag']), (S.SUCCEEDED, GOAL))
                    require(OFFER in json.dumps(view, ensure_ascii=False))
                    require(CAVEAT in json.dumps(view, ensure_ascii=False))
                    require(SOURCE in json.dumps(view, ensure_ascii=False))
                    require_equal(w.server.conversations.task_links(w.session.conversation_id)[-1]['run_id'], run_id)
                    require_equal(len(w.native_launches), 3, 'result reopening repeated a selection')
                    require_equal(len(N.method_rows(root, 'turn/start')), 1, 'result reopening repeated research')


async def t_spoken_goal_question_answer_native_research_assessment_and_voice_result():
    await journey(close_before_result=False)


async def t_same_spoken_journey_finishes_after_voice_end_and_reopens_with_qualifications():
    await journey(close_before_result=True)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
