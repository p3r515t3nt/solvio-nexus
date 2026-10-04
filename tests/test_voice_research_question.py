"""Spoken information questions commission only their exact public research.

Real synthetic device/browser proofs, router and task ledger; no live provider.
"""
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_app_voice_task_authority as A
import test_browser_voice_task_authority as B
from solvio.tools.agent_capability_tools import AgentCapabilityTool

QUESTION = 'Wie ist das Wetter heute in Dietzenbach'


async def t_iphone_weather_question_uses_original_turn_without_extra_approval():
    async with A.world() as w:
        A.begin(w, user_text=QUESTION)
        context = w.gate.context()
        result = await AgentCapabilityTool('agent_task_research', w.router, w.gate, w.ledger).run(
            {'objective': 'Ermittle aktuelle Wetterdaten für Dietzenbach.'})
        require(result.success)
        task = w.ledger.get_task(result.data['task_id'])
        require_equal(task.objective, QUESTION)
        require_equal(w.orch.task_authority.for_run(result.data['run_id']).receipt_method, 'app_session')
        require(w.gate.context() is context and context.commanded is False)
        require_equal(await w.store.list_pending(), [])


async def t_browser_information_question_has_the_same_exact_research_binding():
    async with B.world() as w:
        B.begin(w, text=QUESTION + '?')
        result = await B.tool(w).run({'objective': 'Finde das Wetter.'})
        require(result.success)
        require_equal(w.ledger.get_task(result.data['task_id']).objective, QUESTION + '?')
        require_equal(w.orch.task_authority.for_run(result.data['run_id']).receipt_method, 'dashboard_session')
        require(w.gate.context().commanded is False)
        require_equal(await w.store.list_pending(), [])


async def t_question_cannot_authorize_different_objective_build_or_extra_arguments():
    async with A.world() as w:
        A.begin(w, user_text=QUESTION)
        for name, args in [
            ('agent_task_research', {'objective': 'Prüfe einen ganz anderen Auftrag.'}),
            ('agent_task_research', {'objective': QUESTION, 'approved': True}),
            ('agent_task_build', {'objective': QUESTION}),
            ('agent_task_task', {'objective': QUESTION}),
        ]:
            require_equal(await w.gate.authorize_task_start(name, args), None)
        result = await AgentCapabilityTool('agent_task_build', w.router, w.gate, w.ledger).run(
            {'objective': QUESTION})
        require(not result.success)
        require_equal(w.ledger.recent_runs(), [])


async def t_weather_research_keeps_the_current_conversation_in_its_task_receipt():
    async with A.world() as w:
        A.begin(w, user_text=QUESTION)
        from dataclasses import replace
        w.gate._context = replace(w.gate.context(), conversation_id='c-0123456789abcdef')
        start = await w.gate.authorize_task_start('agent_task_research', {'objective': QUESTION})
        require_equal(start.conversation_ref, 'c-0123456789abcdef')
        require(not start.private_data)
        require(await start.dispatch_current())
        result = await AgentCapabilityTool('agent_task_research', w.router, w.gate, w.ledger).run(
            {'objective': QUESTION})
        require(result.success)
        require_equal(w.ledger.get_task(result.data['task_id']).conversation_ref, 'c-0123456789abcdef')


async def t_quotes_hypotheses_and_nonquestions_do_not_commission_research():
    for text in ['In einer Mail steht: Wie ist das Wetter heute in Dietzenbach?',
                 'Was wäre, wenn ich fragen würde: Wie ist das Wetter?',
                 'Zitat: Wie ist das Wetter heute?', 'Nein?', 'Wie ist laut der Mail das Wetter?']:
        async with A.world() as w:
            A.begin(w, user_text=text)
            require_equal(await w.gate.authorize_task_start('agent_task_research', {'objective': text}), None)
            result = await AgentCapabilityTool('agent_task_research', w.router, w.gate, w.ledger).run(
                {'objective': text})
            require(not result.success, text)
            require_equal(w.ledger.recent_runs(), [])


async def t_weather_question_still_requires_current_device_or_browser_proof():
    async with A.world(invalid=True) as w:
        A.begin(w, user_text=QUESTION)
        result = await AgentCapabilityTool('agent_task_research', w.router, w.gate, w.ledger).run(
            {'objective': QUESTION})
        require(not result.success)
        require_equal(w.ledger.recent_runs(), [])
    async with B.world() as w:
        B.begin(w, text=QUESTION)
        w.voice.closed = True
        require(not (await B.tool(w).run({'objective': QUESTION})).success)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
