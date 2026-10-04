"""Spoken research answers reuse authenticated chat turns and the answer journal.

Synthetic audio/selector/planner only; real session proofs, gates and stores.
"""
import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_voice_personal_task as A
import test_live_chat_followup as C
from solvio.agent_runtime import research_question as Q, store as S, inquiry
from solvio.tools.agent_capability_tools import AgentCapabilityTool

GOAL = 'Suche einen günstigen Flug nach Warschau für eine Person nächste Woche.'
QUESTION = 'Von welchem Flughafen möchtest du abfliegen?'
ANSWER = 'Ab Frankfurt am Main.'


def arguments(ledger, run_id):
    q = Q.view(ledger, run_id)['question']
    return dict(run_id=run_id, question_id=q['id'], expected_revision=q['revision'], expected_digest=q['digest'])


async def prepare(w, browser=False):
    A.prepare(w, browser)
    w.gate._context = replace(w.gate.context(), user_text=GOAL)
    result = await AgentCapabilityTool('agent_task_research', w.router, w.gate, w.ledger).run({'objective': GOAL})
    require(result.success, result.error)
    w.run = w.ledger.get_run(result.data['run_id'])
    w.orch.conversations.add_task_link(w.cid, w.run.task_id, w.run.run_id, source='fixture')
    w.ledger.transition(w.run.run_id, S.PLANNING)
    Q.open_question(w.ledger, w.run.run_id, QUESTION, '{}')
    w.args = arguments(w.ledger, w.run.run_id)
    w.gate._context = replace(w.gate.context(), turn_id='answer-turn', user_text=ANSWER)
    from solvio.tools.task_answer import TaskAnswerTool
    w.tool = TaskAnswerTool(w.dispatcher)
    w.dispatcher.register(w.tool)


async def t_app_and_browser_answer_current_question_without_new_task_grant_or_cost():
    from solvio.realtime.live_session import live_toolkit
    for browser in (False, True):
        async with (A.B.world() if browser else A.A.world()) as w:
            try:
                await prepare(w, browser)
                grant = w.orch.task_authority.for_run(w.run.run_id)
                require('task_answer' in {t['name'] for t in live_toolkit(w.dispatcher, personal=True)})
                require('task_answer' not in {t['name'] for t in live_toolkit(w.dispatcher)})
                first, again = await asyncio.gather(w.tool.run(w.args), w.tool.run(w.args))
                require(first.success and again.success, (first, again))
                require_equal(first.data, again.data)
                require_equal((first.data['task_id'], first.data['run_id']), (w.run.task_id, w.run.run_id))
                require_equal(w.ledger.get_run(w.run.run_id).plan_revision, 1)
                require_equal(len(w.ledger.recent_runs()), 1)
                require_equal(w.orch.task_authority.for_run(w.run.run_id), grant)
                require(ANSWER in Q.context(w.ledger, w.run.run_id))
                require_equal(await w.store.list_pending(), [])
                with w.ledger._open() as db:
                    require_equal(db.execute('SELECT COUNT(*) FROM agent_action_intent_answers').fetchone()[0], 1)
            finally:
                w.orch.conversations.close()


async def t_answer_rejects_foreign_chat_owner_room_model_text_and_stale_question():
    from solvio.capabilities.policy import OriginClass
    async with A.A.world() as w:
        try:
            await prepare(w)
            valid = w.gate.context()
            other,_ = w.orch.conversations.create_conversation(owner_principal='local-owner',client_request_id='other-research-chat')
            for ctx in (replace(valid, conversation_id=other['conversation_id']),
                        replace(valid, principal='foreign'), replace(valid, app_task_session=None),
                        replace(valid, origin=OriginClass.ROOM_VOICE), replace(valid, commanded=False)):
                w.gate._context = ctx
                require(not (await w.tool.run(w.args)).success)
            w.gate._context = valid
            for args in (w.args | {'answer':'Berlin'}, w.args | {'approved':True},
                         w.args | {'expected_digest':'f'*64}, w.args | {'expected_revision':2}):
                require(not (await w.tool.run(args)).success)
            require_equal(w.ledger.get_run(w.run.run_id).state, S.WAITING_USER)
        finally:
            w.orch.conversations.close()


async def t_answer_rechecks_session_turn_chat_and_question_after_authorization_await():
    for loss in ('session','turn','chat','question','cancel'):
        async with A.A.world() as w:
            try:
                await prepare(w)
                real = w.gate.authorize_task_start
                async def lost(*args, **kwargs):
                    binding = await real(*args, **kwargs)
                    if loss == 'session': w.socket.closed = True
                    if loss == 'turn': w.gate.clear()
                    if loss == 'chat': w.orch.conversations.delete_conversation(w.cid)
                    if loss == 'question': w.args['expected_digest'] = 'e'*64
                    if loss == 'cancel': await w.orch.cancel(w.run.run_id)
                    return binding
                with patch.object(w.gate, 'authorize_task_start', lost):
                    result = await w.tool.run(w.args)
                # The body is copied before awaits; changing model input cannot
                # change the already bound answer. All authority losses refuse.
                require_equal(result.success, loss == 'question')
                require_equal(w.ledger.get_run(w.run.run_id).plan_revision, int(loss == 'question'))
            finally:
                w.orch.conversations.close()


async def t_new_turn_cannot_replay_an_old_answer_or_answer_next_question_with_stale_id():
    async with A.A.world() as w:
        try:
            await prepare(w)
            require((await w.tool.run(w.args)).success)
            w.gate._context = replace(w.gate.context(), turn_id='another-turn')
            require(not (await w.tool.run(w.args)).success)
            Q.open_question(w.ledger, w.run.run_id, 'Welches Datum?', '{}')
            require(not (await w.tool.run(w.args)).success)
            require_equal(w.ledger.get_run(w.run.run_id).state, S.WAITING_USER)
            require_equal(w.ledger.get_run(w.run.run_id).plan_revision, 1)
        finally:
            w.orch.conversations.close()


def t_status_exposes_exact_question_without_turning_it_into_a_new_authority():
    import test_research_question as R
    with R.world() as w:
        R.questions(w); asyncio.run(R.drive(w))
        view = inquiry.run_view(w.ledger, w.ledger.get_run(w.run.run_id))
        require_equal(view['research_question'], Q.view(w.ledger,w.run.run_id)['question'])


SCRIPT = '''import json,sys
p=json.loads(sys.stdin.read().split('\\n',1)[1]);u=json.loads(p[1]['content']);rows=u.get('core_results',[])
if u['user_text'].startswith('Suche'):
 reply={'calls':[{'name':'agent_task_research','arguments':{'objective':u['user_text']}}],'clarification':''}
elif not rows:
 reply={'calls':[{'name':'agent_task_status','arguments':{'scope':'current_question'}}],'clarification':''}
else:
 found=rows[0]['result']['data']
 if found['found'] and found['auftrag'].get('research_question'):
  v=found['auftrag'];q=v['research_question']
  reply={'calls':[{'name':'task_answer','arguments':{'run_id':v['kennung'],'question_id':q['id'],'expected_revision':q['revision'],'expected_digest':q['digest']}}],'clarification':''}
 else:reply={'calls':[],'clarification':'Welchen Auftrag meinst du?'}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''


async def t_live_question_spoken_answer_and_later_result_stay_in_one_chat_and_run():
    from solvio.realtime import live_session as L
    from solvio.tools.task_answer import TaskAnswerTool
    # C.world starts a real browser WebSocket and the native selector protocol.
    with patch.object(C, 'SCRIPT', SCRIPT), patch.object(L,'RESEARCH_POLL_SECONDS',.01):
        async with C.world() as w:
            w.server.dispatcher.register(TaskAnswerTool(w.server.dispatcher))
            await C.say(w, GOAL, 'start-research')
            accepted = w.session._tool_results[-1]
            require(accepted['success'], accepted)
            run_id = accepted['data']['run_id']
            grant = w.orch.task_authority.for_run(run_id)
            w.ledger.transition(run_id, S.PLANNING)
            Q.open_question(w.ledger, run_id, QUESTION, '{}')
            def output():
                return '\n'.join(e['content'] for e in w.provider.sent if e['type']=='session.commentary.append')
            await C.C.H.until(lambda: QUESTION in output())
            require_equal(output().count(QUESTION),1)
            # The real phone used a chat containing older jobs. They must not
            # obscure the sole open question or become the answer target.
            w.orch.conversations.add_task_link(w.session.conversation_id,
                w.run.task_id, w.run.run_id, source='older-unrelated-job')
            await C.say(w, ANSWER, 'answer-status', 2000)
            # Real incident: the voice provider never emitted another callback.
            # One answer must complete the bounded status -> answer handoff.
            answer = w.session._tool_results[-1]
            require(answer['success'], answer)
            require_equal(answer['data']['run_id'], run_id)
            require(ANSWER in Q.context(w.ledger, run_id))
            require_equal(w.orch.task_authority.for_run(run_id), grant)
            require_equal(len(w.native_launches), 3)  # start, read, one answer selection
            second = C.C.payload(w.native_launches[-1])
            require_equal(second['user_text'], ANSWER)
            require_equal(second['core_results'][0]['result']['data']['auftrag']['kennung'],run_id)
            require(w.session._read_continuation is None)
            await C.notice(w, 'late-provider-callback', 3000)
            require_equal(len(w.native_launches), 3)
            require_equal(w.ledger.get_run(run_id).plan_revision, 1)
            w.ledger.transition(run_id, S.RUNNING)
            w.ledger.transition(run_id, S.SUCCEEDED, result_summary='Der lokale Testvergleich ist fertig.')
            await C.C.H.until(lambda:'Der lokale Testvergleich ist fertig.' in output())
            require_equal(len(w.ledger.runs_for_task(accepted['data']['task_id'])),1)
            await C.C.H.end(w,w.client)
            # Internal cost/selection keys are not provider delegation IDs.
            require(all(e.get('delegation_id') in (None, 'start-research', 'answer-status', 'late-provider-callback')
                for e in w.provider.sent if e['type'].endswith('.append') and 'content' in e),
                'automatic answer used an unknown provider delegation ID')


async def t_open_question_lookup_excludes_old_foreign_and_nonquestion_tasks_but_keeps_ambiguity():
    from solvio.realtime.live_status_tools import CoreStatusTool
    from solvio.realtime.control import CoreControl
    async with A.A.world() as w:
        try:
            await prepare(w)
            first = w.run.run_id
            status = CoreStatusTool(CoreControl(w.dispatcher),'agent_task_status')
            found = await status.run({'scope':'current_question'})
            require_equal(found.data['auftrag']['kennung'],first)
            for owner in ('foreign', 'local-owner'):
                task = w.ledger.create_task(objective='Unrelated fixture',scope='research',
                    created_origin='trusted_interactive_app',created_principal=owner,conversation_ref=w.cid)
                run = w.ledger.create_run(task_id=task.task_id)
                w.orch.conversations.add_task_link(w.cid,task.task_id,run.run_id,source='fixture')
            require_equal((await status.run({'scope':'current_question'})).data['auftrag']['kennung'],first)
            # A second independently admitted question must remain ambiguous.
            w.gate._context=replace(w.gate.context(),turn_id='second-job',user_text='Suche Flug nach Paris.')
            start=await AgentCapabilityTool('agent_task_research',w.router,w.gate,w.ledger).run({'objective':'Suche Flug nach Paris.'})
            require(start.success,start)
            w.orch.conversations.add_task_link(w.cid,start.data['task_id'],start.data['run_id'],source='fixture')
            w.ledger.transition(start.data['run_id'],S.PLANNING)
            Q.open_question(w.ledger,start.data['run_id'],'An welchem Tag?','{}')
            ambiguous=await status.run({'scope':'current_question'})
            require(ambiguous.data['ambiguous']); require_equal(ambiguous.data['gesamt'],2)
            require_equal(w.ledger.get_run(first).state,S.WAITING_USER)
        finally:
            w.orch.conversations.close()


@asynccontextmanager
async def drained_world():
    # Cancelling to_thread does not stop its SQLite read. Own those fixture
    # futures until they have really finished before removing the temp store.
    async with C.world() as w:
        pending = set()
        original = asyncio.to_thread
        async def tracked(fn, *args, **kwargs):
            task = asyncio.create_task(original(fn, *args, **kwargs))
            pending.add(task)
            task.add_done_callback(pending.discard)
            return await asyncio.shield(task)
        with patch.object(asyncio, 'to_thread', tracked):
            try:
                yield w
            finally:
                if w.session.active:
                    await C.C.H.end(w, w.client)
                await asyncio.gather(*pending, return_exceptions=True)


async def t_automatic_answer_selection_rechecks_correction_close_and_stale_question():
    from solvio.realtime import live_session as L
    from solvio.tools.task_answer import TaskAnswerTool
    delayed=SCRIPT.replace("rows=u.get('core_results',[])","rows=u.get('core_results',[]);import time;time.sleep(.2 if rows else 0)")
    for loss in ('correction','close','question'):
      with patch.object(C,'SCRIPT',delayed),patch.object(L,'RESEARCH_POLL_SECONDS',.01):
        async with drained_world() as w:
            w.server.dispatcher.register(TaskAnswerTool(w.server.dispatcher))
            await C.say(w,GOAL,'start')
            run_id=w.session._tool_results[-1]['data']['run_id']
            w.ledger.transition(run_id,S.PLANNING); Q.open_question(w.ledger,run_id,QUESTION,'{}')
            speaking=asyncio.create_task(C.say(w,ANSWER,'answer',2000))
            await C.C.H.until(lambda:len(w.native_launches)==3)
            if loss=='correction':
                await w.provider.events.put(C.C.H.transcript('Nein, warte.',4000,4500,'new-input'))
                await C.C.H.until(lambda:bool(w.session._input_parts))
            elif loss=='close': await C.C.H.end(w,w.client)
            else:
                # Another authenticated answer wins while selection is pending.
                other=await w.server.dispatcher.tool('task_answer').run(arguments(w.ledger,run_id))
                require(other.success,other)
                Q.open_question(w.ledger,run_id,'Welches andere Datum?','{}')
            await speaking
            require_equal(w.ledger.get_run(run_id).state,S.WAITING_USER)
            require_equal(w.ledger.get_run(run_id).plan_revision,int(loss=='question'))
            with w.ledger._open() as db:
                require_equal(db.execute('SELECT COUNT(*) FROM agent_action_intent_answers').fetchone()[0],int(loss=='question'))
            require(w.session._read_continuation is None)
            if loss!='close': await C.C.H.end(w,w.client)


async def t_automatic_answer_is_bound_to_unique_fresh_status_even_with_other_valid_question():
    from solvio.realtime import live_session as L
    from solvio.tools.task_answer import TaskAnswerTool
    for ambiguous in (False,True):
      with patch.object(C,'SCRIPT',SCRIPT),patch.object(L,'RESEARCH_POLL_SECONDS',.01):
        async with C.world() as w:
            w.server.dispatcher.register(TaskAnswerTool(w.server.dispatcher))
            runs=[]
            for index in range(2):
                await C.say(w,GOAL+str(index),'start'+str(index),index*2000)
                rid=w.session._tool_results[-1]['data']['run_id'];runs.append(rid)
                w.ledger.transition(rid,S.PLANNING);Q.open_question(w.ledger,rid,QUESTION,'{}')
            other_args=arguments(w.ledger,runs[1])
            backend=w.server.dispatcher.live_backend; choose=backend.choose
            async def spoof(snapshot):
                result=await choose(snapshot)
                if result.calls and result.calls[0]['name']=='agent_task_status' and not ambiguous:
                    return replace(result,calls=({'name':'agent_task_status','arguments':{
                        'scope':'current_question','key':runs[0]}},))
                if result.calls and result.calls[0]['name']=='task_answer':
                    return replace(result,calls=({'name':'task_answer','arguments':other_args},))
                return result
            before=len(w.native_launches)
            with patch.object(backend,'choose',spoof):
                await C.say(w,ANSWER,'answer',5000)
            require_equal(len(w.native_launches)-before,1 if ambiguous else 2)
            require(all(w.ledger.get_run(r).state==S.WAITING_USER for r in runs))
            with w.ledger._open() as db:
                require_equal(db.execute('SELECT COUNT(*) FROM agent_action_intent_answers').fetchone()[0],0)
            require(w.session._read_continuation is None)
            await C.C.H.end(w,w.client)


async def t_automatic_answer_selection_cannot_loop_start_cancel_or_bypass_quota():
    from solvio.realtime import live_session as L
    from solvio.tools.task_answer import TaskAnswerTool
    marker="found=rows[0]['result']['data']"
    for mode in ('read','cancel','start','quota'):
        if mode=='quota':
            injected="print(json.dumps({'type':'turn.failed','error':{'message':'Usage limit reached'}}));sys.exit(0)"
        else:
            call={'read':{'name':'agent_task_status','arguments':{'scope':'current_question'}},
                  'cancel':{'name':'agent_run_cancel','arguments':{'run_id':'fake'}},
                  'start':{'name':'agent_task_research','arguments':{'objective':'Another search'}}}[mode]
            injected="print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps("+repr({'calls':[call],'clarification':''})+")}}));print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}));sys.exit(0)"
        script=SCRIPT.replace(marker,injected+'\n '+marker)
        with patch.object(C,'SCRIPT',script),patch.object(L,'RESEARCH_POLL_SECONDS',.01):
          async with C.world() as w:
            w.server.dispatcher.register(TaskAnswerTool(w.server.dispatcher))
            await C.say(w,GOAL,'start')
            run_id=w.session._tool_results[-1]['data']['run_id']
            w.ledger.transition(run_id,S.PLANNING);Q.open_question(w.ledger,run_id,QUESTION,'{}')
            await C.say(w,ANSWER,'answer',2000)
            require_equal(w.ledger.get_run(run_id).state,S.WAITING_USER)
            require_equal(len(w.native_launches),3)
            require_equal(len(w.ledger.recent_runs()),2)
            require(w.session._read_continuation is None)
            await C.notice(w,'no-repeat',3000)
            require_equal(len(w.native_launches),3)
            await C.C.H.end(w,w.client)


async def t_attested_spoken_answer_reaches_native_planner_worker_and_assessor():
    from test_agent_public_research import world, PLAN
    from test_agent_task_entry import _chat_store
    from test_voice_task_followup import iphone_tool
    from solvio.tools.task_answer import TaskAnswerTool
    from solvio.agent_runtime import cost_dispatch as CD
    question_plan = dict(PLAN, schritte=[], rueckfrage=QUESTION)
    async with world(plan=question_plan, followup_plan=PLAN) as w:
        store = _chat_store(w, w.folder)
        w.orch.conversations = store
        try:
            chat,_ = store.create_conversation(owner_principal='local-owner', client_request_id='spoken-research-chat')
            cid = chat['conversation_id']
            response = await w.start(dict(scope='research', objective=w.objective, target_repo='',
                client_request_id='spoken-research-start', conversation_ref=cid))
            require_equal(response.status,201)
            row = await response.json(); run_id = row['run_id']
            grant = w.orch.task_authority.for_run(run_id)
            await w.tick_until(run_id,S.WAITING_USER)
            async def enroll(**kw):
                return await A.A.H.enroll_attested(w.cp, **kw), None, None
            w.device = enroll
            source,gate,socket = await iphone_tool(w,cid)
            gate._context = replace(gate.context(),user_text=ANSWER)
            tool = TaskAnswerTool(source.dispatcher)
            accepted = await tool.run(arguments(w.ledger,run_id))
            require(accepted.success,accepted)
            socket.closed=True; gate.clear()
            final = await w.tick_until(run_id,S.SUCCEEDED)
            require_equal(final.task_id,row['task_id'])
            require_equal(w.orch.task_authority.for_run(run_id),grant)
            require_equal(len(w.ledger.runs_for_task(final.task_id)),1)
            require(ANSWER in w.calls()[1]['request']['kontext'])
            require(ANSWER in json.loads(w.calls()[-1]['request']['ergebnis'])['nutzerangaben'])
            require_equal([r['phase'] for r in CD.invocations(w.ledger,final.task_id)],
                ['plan','plan','specialist','assessment'])
        finally:
            store.close()


async def t_internal_replanning_keeps_voice_result_observer():
    import test_live_research_result as R
    async with R.world() as w:
        w.ledger.set_run_fields(w.run.run_id, plan_revision=1)
        R.finish(w)
        await R.H.until(lambda:R.WEATHER in R.output(w))
        require_equal(R.output(w).count(R.WEATHER),1)


async def t_native_first_and_answer_selection_outlast_idle_without_losing_same_question():
    from solvio.realtime import live_session as L
    from solvio.tools.task_answer import TaskAnswerTool
    delayed = SCRIPT.replace("rows=u.get('core_results',[])",
        "rows=u.get('core_results',[]);import time;time.sleep(.16)")
    with patch.object(C, 'SCRIPT', delayed), patch.object(L, 'SELECTION_WAIT_SECONDS', .4), \
            patch.object(L, 'READ_CONTINUATION_SECONDS', .4), patch.object(L, 'RESEARCH_POLL_SECONDS', .01):
      async with drained_world() as w:
        w.server.idle_timeout = .07
        w.server.dispatcher.register(TaskAnswerTool(w.server.dispatcher))
        await C.say(w, GOAL, 'slow-start')
        require(w.session.active, 'A real local selector may finish after the ordinary idle interval')
        accepted = w.session._tool_results[-1]
        require(accepted['success'], accepted)
        run_id = accepted['data']['run_id']
        grant = w.orch.task_authority.for_run(run_id)
        w.ledger.transition(run_id, S.PLANNING)
        Q.open_question(w.ledger, run_id, QUESTION, '{}')
        await C.C.H.until(lambda: not w.session._research_watchers)
        await C.say(w, ANSWER, 'slow-answer', 2000)
        require(w.session.active)
        require_equal(w.session._tool_results[-1]['data']['run_id'], run_id)
        require_equal(w.orch.task_authority.for_run(run_id), grant)
        require_equal(w.ledger.get_run(run_id).plan_revision, 1)
        require(ANSWER in Q.context(w.ledger, run_id))
        require_equal(len(w.native_launches), 3)
        with w.ledger._open() as db:
            require_equal(db.execute('SELECT COUNT(*) FROM agent_action_intent_answers').fetchone()[0], 1)
        await C.notice(w, 'late-after-slow-answer', 3000)
        require_equal(len(w.native_launches), 3)
        await C.C.H.end(w, w.client)


async def t_native_selection_timeout_reaps_child_keeps_unknown_cost_and_chat_without_retry():
    from solvio.realtime import live_session as L
    with tempfile.TemporaryDirectory(prefix='solvio-selector-timeout-') as folder:
        pid_file = Path(folder) / 'selector.pid'
        child_file = Path(folder) / 'child.pid'
        script = ('import os,sys,time,subprocess;sys.stdin.read();'
            + 'open(' + repr(str(pid_file)) + ',"w").write(str(os.getpid()));'
            + 'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
            + 'open(' + repr(str(child_file)) + ',"w").write(str(p.pid));time.sleep(60)')
        with patch.object(C, 'SCRIPT', script), patch.object(L, 'SELECTION_WAIT_SECONDS', .3):
          async with drained_world() as w:
            w.server.idle_timeout = .07
            await C.say(w, GOAL, 'hung-selector')
            require_equal(len(w.native_launches), 1)
            require_equal(len(w.ledger.recent_runs()), 1)  # unrelated fixture only
            for file in (pid_file, child_file):
                require(file.exists(), 'The real local child must actually have started')
                pid = int(file.read_text())
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError('selector or process-group child survived cancellation')
            with w.ledger._open() as db:
                claims = list(db.execute('SELECT state FROM agent_provider_invocations'))
                money = list(db.execute('SELECT state FROM agent_cost_reservations'))
            require_equal([r[0] for r in claims], ['unknown'])
            require_equal([r[0] for r in money], ['unknown'])
            await w.session._persist_queue.join()
            rows = w.server.conversations.recent_context(w.session.conversation_id)
            require_equal(sum(r['role'] == 'assistant' and r['text'] == L.SELECTION_TIMEOUT_TEXT for r in rows), 1)
            await C.notice(w, 'no-repeat-after-timeout', 1000)
            require_equal(len(w.native_launches), 1)
            await C.C.H.end(w, w.client)


async def t_native_first_selection_rechecks_corrected_source_revoke_and_explicit_end():
    from solvio.realtime import live_session as L
    delayed = SCRIPT.replace("rows=u.get('core_results',[])",
        "rows=u.get('core_results',[]);import time;time.sleep(.18)")
    for loss in ('correction', 'revoke', 'end'):
      with patch.object(C, 'SCRIPT', delayed), patch.object(L, 'SELECTION_WAIT_SECONDS', .4):
        async with drained_world() as w:
            saying = asyncio.create_task(C.say(w, GOAL, 'pending-start'))
            await C.C.H.until(lambda: len(w.native_launches) == 1)
            if loss == 'correction':
                await w.provider.events.put(C.C.H.transcript('Nein, noch nichts starten.', 1500, 2000, 'corrected'))
                await C.C.H.until(lambda: w.session._revision > 1)
            elif loss == 'revoke':
                await w.service.revoke(w.auth['session_id'], principal='local-owner')
            else:
                await C.C.H.end(w, w.client)
            await saying
            require_equal(len(w.ledger.recent_runs()), 1)
            require_equal(len(w.native_launches), 1)
            if loss != 'end' and w.session.active:
                await C.C.H.end(w, w.client)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
