"""Public Live callbacks and real Core lookup/admission; selector/audio are fixtures.

This proves composition and chat binding, not real model intent quality.
No production data or provider is contacted. Run IDs reach the selector only
through the actual Core status response, never through its fixture prompt.
"""
import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
import sys
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_continuation as C
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.tools.personal_task import PersonalTaskTool
from solvio.tools.task_continue import TaskContinueTool
from solvio.agent_runtime import store as S, result_files as RF, requirements as RQ, inquiry
import json

GOAL = 'Lies die letzte Rechnung und formuliere eine Rückfrage als Textdatei. Nichts versenden.'
FOLLOWUP = 'Mach das Ergebnis bitte kürzer. Nichts versenden.'
SCRIPT = '''import json,sys
p=json.loads(sys.stdin.read().split('\\n',1)[1]);u=json.loads(p[1]['content'])
rows=u.get('core_results',[])
if u['user_text'].startswith('Lies'):
 reply={'calls':[{'name':'personal_task','arguments':{}}],'clarification':''}
elif not rows:
 reply={'calls':[{'name':'agent_task_status','arguments':{'scope':'current_chat'}}],'clarification':''}
else:
 found=rows[0]['result']['data']
 if found['found']:
  reply={'calls':[{'name':'task_continue','arguments':{'run_id':found['auftrag']['kennung']}}],'clarification':''}
 else:
  reply={'calls':[],'clarification':'Welches fertige Ergebnis aus diesem Chat meinst du?'}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''


@asynccontextmanager
async def world(*, script=None):
    async def issued(w, client=None, headers=None):
        chat, _ = w.server.conversations.create_conversation(owner_principal='local-owner', client_request_id='live-explicit-chat')
        response = await (client or w.client).post(C.H.H.V.SESSION_PATH,
            json={'conversation_id': chat['conversation_id']}, headers=headers or w.headers)
        require_equal(response.status, 201)
        return await response.json()
    with patch.object(C.H.H, 'issued', issued):
      async with C.world(script_override=SCRIPT if script is None else script) as w:
          w.orch.conversations = w.server.conversations
          d = w.server.dispatcher
          d.capabilities = w.router
          w.router.register(SPECS['agent_task_task'], AgentCapabilities(w.orch).task)
          d.register(PersonalTaskTool(d)); d.register(TaskContinueTool(d))
          # Finish the real result observer before temporary-store teardown;
          # cancelling its coroutine cannot reap an already-running DB thread.
          with (patch.object(C.H.L, 'RESEARCH_WAIT_SECONDS', .03) if script is not None else nullcontext()), \
                  (patch.object(C.H.L, 'RESEARCH_POLL_SECONDS', .005) if script is not None else nullcontext()):
              yield w


async def say(w, text, key, start=0):
    await w.provider.events.put(C.H.transcript(text, start, start + 1000, key))
    await w.provider.events.put(C.H.delegate(key, start + 1000))
    await C.H.until(lambda: key in w.session._delegations)
    await asyncio.wait_for(w.session._tool_queue.join(), 5)


async def notice(w, key, offset):
    await w.provider.events.put(C.H.delegate(key, offset))
    await C.H.until(lambda: key in w.session._delegations)
    await asyncio.wait_for(w.session._tool_queue.join(), 5)


async def completed(w, *, key='personal-start', start=0):
    await say(w, GOAL, key, start)
    accepted = w.session._tool_results[-1]
    require(accepted['success'], accepted)
    run = w.ledger.get_run(accepted['data']['run_id'])
    task = w.ledger.get_task(run.task_id)
    require_equal(task.objective, GOAL)
    require_equal(task.conversation_ref, w.session.conversation_id)
    criteria = RQ.validate({'auskunft': [{'id': 'r1', 'text': 'Formuliere eine Rückfrage.'}]}, objective=task.objective)
    w.ledger.bind_requirements(task.task_id, json.dumps(criteria))
    w.ledger.transition(run.run_id, S.PLANNING); w.ledger.transition(run.run_id, S.RUNNING)
    step = w.ledger.create_step(run_id=run.run_id, seq=1, kind='specialist', specialist_profile='worker/codex')
    w.ledger.update_step(step.step_id, state='running', started=True)
    file = RF.publish_file(w.ledger, run.run_id, step.step_id, b'Welche Frist gilt fuer die Rechnung?',
        'antwort.txt', 'text/plain', requirement='r1')
    w.ledger.update_step(step.step_id, state='succeeded', finished=True)
    w.ledger.transition(run.run_id, S.SUCCEEDED, result_summary='Der Antworttext ist fertig.')
    w.ledger.set_task_state(task.task_id, S.TASK_COMPLETED)
    return run, file


async def t_live_personal_start_then_status_then_spoken_revision_uses_current_chat():
    async with world() as w:
        original, file = await completed(w)
        # A different task already exists in C.world; global lookup is ambiguous.
        global_status = await w.server.dispatcher.tool('agent_task_status').control.handle({'op':'agent_task_status'})
        require(global_status['ambiguous'])
        await say(w, FOLLOWUP, 'read-this-chat', 2000)
        result = w.session._tool_results[-1]
        require(result['success'], result)
        require_equal((result['data']['task_id'], result['data']['revision']), (original.task_id, 2))
        require_equal(len(w.ledger.runs_for_task(original.task_id)), 2)
        require_equal(w.ledger.get_run(w.run.run_id).state, S.CREATED)
        prompt = C.payload(w.native_launches[-1])
        require_equal(prompt['user_text'], FOLLOWUP)
        actual = prompt['core_results'][0]['result']
        require(actual['success'], actual)
        require_equal(actual['data']['auftrag']['kennung'], original.run_id)
        require_equal(RF.read_result(w.ledger, original.run_id, file['id'])[1], b'Welche Frist gilt fuer die Rechnung?')
        require(any(link['run_id'] == result['data']['run_id'] for link in w.orch.conversations.task_links(w.session.conversation_id)))
        require_equal(await w.store.list_pending(), [])
        before = len(w.native_launches)
        await notice(w, 'late-continue-this-chat', 3000)
        require_equal(len(w.native_launches), before)
        require_equal(len(w.ledger.runs_for_task(original.task_id)), 2)
        await C.H.end(w, w.client)
        require_equal(w.ledger.get_run(result['data']['run_id']).state, S.CREATED)


async def t_automatic_chat_followup_refuses_another_valid_result_from_same_chat():
    async with world() as w:
        first_run, _ = await completed(w)
        second_run, _ = await completed(w, key='second-result', start=2000)
        backend = w.server.dispatcher.live_backend
        choose = backend.choose
        async def spoof(snapshot):
            result = await choose(snapshot)
            if result.calls and result.calls[0]['name'] == 'agent_task_status':
                return replace(result, calls=({'name':'agent_task_status','arguments':{
                    'scope':'current_chat','key':first_run.run_id}},))
            if result.calls and result.calls[0]['name'] == 'task_continue':
                return replace(result, calls=({'name':'task_continue','arguments':{'run_id':second_run.run_id}},))
            return result
        before = len(w.native_launches)
        with patch.object(backend, 'choose', spoof):
            await say(w, FOLLOWUP, 'choose-wrong-existing-result', 4000)
        require_equal(len(w.native_launches) - before, 2)
        require_equal(len(w.ledger.runs_for_task(first_run.task_id)), 1)
        require_equal(len(w.ledger.runs_for_task(second_run.task_id)), 1)
        require(w.session._read_continuation is None)
        await C.H.end(w, w.client)


async def t_automatic_chat_followup_rechecks_correction_source_revoke_and_close():
    delayed = SCRIPT.replace("rows=u.get('core_results',[])",
        "rows=u.get('core_results',[]);import time;time.sleep(.18 if rows else 0)")
    for loss in ('correction', 'revoke', 'close'):
      with patch.dict(globals(), {'SCRIPT': delayed}):
        async with world() as w:
            original, _ = await completed(w)
            before = len(w.native_launches)
            changing = asyncio.create_task(say(w, FOLLOWUP, 'pending-change', 2000))
            await C.H.until(lambda: len(w.native_launches) == before + 2)
            if loss == 'correction':
                await w.provider.events.put(C.H.transcript('Nein, warte.', 4000, 4500, 'new-input'))
                await C.H.until(lambda: bool(w.session._input_parts))
            elif loss == 'revoke':
                await w.service.revoke(w.auth['session_id'], principal='local-owner')
            else:
                await C.H.end(w, w.client)
            await changing
            require_equal(len(w.ledger.runs_for_task(original.task_id)), 1)
            require(w.session._read_continuation is None)
            if loss != 'close' and w.session.active:
                await C.H.end(w, w.client)


async def t_empty_chat_does_not_pick_the_only_task_from_another_conversation():
    async with world() as w:
        await say(w, FOLLOWUP, 'empty-chat')
        status = w.session._tool_results[-1]
        require(status['success'], status)
        require_equal(status['data']['gesamt'], 0)
        require_equal(len(w.ledger.recent_runs()), 1)
        require(any('Welches fertige Ergebnis' in json.dumps(row, ensure_ascii=False) for row in w.provider.sent))
        require(w.session._read_continuation is None)
        await C.H.end(w, w.client)


async def t_several_current_chat_tasks_remain_ambiguous_and_do_not_start_revision():
    async with world() as w:
        original, _ = await completed(w)
        w.orch.conversations.add_task_link(w.session.conversation_id, w.run.task_id, w.run.run_id, source='fixture')
        await say(w, FOLLOWUP, 'ambiguous-chat', 2000)
        status = w.session._tool_results[-1]['data']
        require(status['ambiguous']); require_equal(status['gesamt'], 2)
        require_equal(len(w.ledger.runs_for_task(original.task_id)), 1)
        require(w.session._read_continuation is None)
        await C.H.end(w, w.client)


async def t_linked_old_result_survives_global_recent_limit_and_foreign_keys_stay_out():
    async with world() as w:
        original, _ = await completed(w)
        for number in range(30):
            task = w.ledger.create_task(objective='Anderer künstlicher Auftrag '+str(number), scope='research',
                created_origin='trusted_dashboard', created_principal='local-owner')
            w.ledger.create_run(task_id=task.task_id)
        await say(w, FOLLOWUP, 'older-than-recent', 2000)
        require_equal(C.payload(w.native_launches[-1])['core_results'][0]['result']['data']['auftrag']['kennung'], original.run_id)
        require_equal(w.session._tool_results[-1]['data']['task_id'], original.task_id)
        require_equal(len(w.ledger.runs_for_task(original.task_id)), 2)
        tool = w.server.dispatcher.tool('agent_task_status')
        miss = await tool.run({'scope':'current_chat', 'key':w.run.run_id})
        require(miss.success); require(not miss.data['found'])
        require(all(row['kennung'] != w.run.run_id for row in miss.data['candidates']))
        await C.H.end(w, w.client)


async def t_scoped_reader_rejects_missing_foreign_room_or_lost_source_without_global_fallback():
    from solvio.capabilities.policy import OriginClass
    async with world() as w:
        await completed(w)
        tool = w.server.dispatcher.tool('agent_task_status')
        valid = w.gate.context()
        foreign,_ = w.orch.conversations.create_conversation(owner_principal='foreign-owner', client_request_id='foreign-read')
        for context in (replace(valid, conversation_id=''), replace(valid, conversation_id=foreign['conversation_id']),
                        replace(valid, origin=OriginClass.ROOM_VOICE), replace(valid, browser_task_session=None),
                        replace(valid, session_id='other-session')):
            w.gate._context = context
            require(not (await tool.run({'scope':'current_chat'})).success)
        w.gate._context = valid
        real = inquiry.find_linked
        def lost(*args, **kwargs):
            result = real(*args, **kwargs)
            w.gate.clear()
            return result
        with patch.object(inquiry, 'find_linked', side_effect=lost):
            result = await tool.run({'scope':'current_chat'})
        require(not result.success); require(result.data is None)
        require(not (await tool.run({'scope':'invented'})).success)
        await C.H.end(w, w.client)


async def t_attested_iphone_can_read_without_command_authority_but_not_after_close():
    import test_voice_personal_task as A
    from solvio.realtime.live_status_tools import CoreStatusTool
    from solvio.realtime.control import CoreControl
    async with A.A.world() as w:
        start = A.prepare(w)
        try:
            accepted = await start.run({})
            require(accepted.success)
            tool = CoreStatusTool(CoreControl(w.dispatcher), 'agent_task_status')
            w.gate._context = replace(w.gate.context(), commanded=False)
            result = await tool.run({'scope':'current_chat'})
            require(result.success, result.error)
            require_equal(result.data['auftrag']['kennung'], accepted.data['run_id'])
            w.socket.closed = True
            require(not (await tool.run({'scope':'current_chat'})).success)
        finally:
            w.orch.conversations.close()


async def t_foreign_task_link_and_newer_unlinked_revision_are_not_returned():
    async with world() as w:
        original, _ = await completed(w)
        cid = w.session.conversation_id
        foreign = w.ledger.create_task(objective='Fremdes Ergebnis', scope='research',
            created_origin='trusted_dashboard', created_principal='foreign-owner')
        run = w.ledger.create_run(task_id=foreign.task_id)
        w.orch.conversations.add_task_link(cid, foreign.task_id, run.run_id, source='fixture')
        tool = w.server.dispatcher.tool('agent_task_status')
        result = await tool.run({'scope':'current_chat'})
        require_equal(result.data['gesamt'], 1)
        require_equal(result.data['auftrag']['kennung'], original.run_id)
        w.ledger.create_run(task_id=original.task_id)
        result = await tool.run({'scope':'current_chat'})
        require_equal(result.data['gesamt'], 0)
        await C.H.end(w, w.client)


FLIGHT_REQUEST = ('Könntest du mir bitte den günstigsten Flug für nächste Woche nach Warschau '
    'von Frankfurt für eine Person Hinflug mal raussuchen')


def research_script(mode='research', *, delay=0):
    # A real local CLI initially reads chat status, then chooses from the
    # actual original text and exact Core result in its second stdin. This
    # reproduces the observed routing error, not a model-quality benchmark.
    return '''import json,sys,time
p=json.loads(sys.stdin.read().split('\\n',1)[1]);u=json.loads(p[1]['content'])
rows=u.get('core_results',[]); mode=MODE
if rows: time.sleep(DELAY)
call={'name':'agent_task_research','arguments':{'objective':u['user_text'].strip()}}
if not rows and mode!='direct':
 call={'name':'agent_task_status','arguments':{'scope':'current_chat'}}
elif rows:
 if mode=='repeat': call={'name':'agent_task_status','arguments':{'scope':'current_chat'}}
 elif mode=='wrong_goal': call['arguments']['objective']='Recherchiere ein anderes Hotel.'
 elif mode=='personal': call={'name':'personal_task','arguments':{}}
 elif mode=='mail': call={'name':'mail_send','arguments':{'to':'fixture','subject':'fixture','body':'fixture'}}
 elif mode=='wrong_continue':
  found=rows[0]['result']['data']; target=found.get('auftrag',{}).get('kennung')
  target=target or found['candidates'][0]['kennung']
  call={'name':'task_continue','arguments':{'run_id':target}}
reply={'calls':[call],'clarification':''}
if rows and mode=='mixed': reply['calls'].append({'name':'agent_task_status','arguments':{'scope':'current_chat'}})
if rows and mode=='observation': reply={'calls':[],'clarification':'','observation':True}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''.replace('MODE', repr(mode)).replace('DELAY', repr(delay))


def old_research(w, states=(S.FAILED, S.SUCCEEDED, S.FAILED)):
    from dataclasses import asdict
    old = []
    for number, state in enumerate(states):
        task = w.ledger.create_task(objective='Alte künstliche Frankfurt-Warschau-Recherche '+str(number),
            scope='research', created_origin='trusted_dashboard', created_principal='local-owner',
            conversation_ref=w.session.conversation_id)
        run = w.ledger.create_run(task_id=task.task_id)
        w.ledger.transition(run.run_id, S.PLANNING)
        w.ledger.transition(run.run_id, S.RUNNING)
        w.ledger.transition(run.run_id, state, result_summary='Alter künstlicher Recherchebefund.')
        w.ledger.set_task_state(task.task_id, S.TASK_COMPLETED if state == S.SUCCEEDED else S.TASK_FAILED)
        w.orch.conversations.add_task_link(w.session.conversation_id, task.task_id, run.run_id, source='fixture')
        old.append((asdict(w.ledger.get_task(task.task_id)), asdict(w.ledger.get_run(run.run_id))))
    w.session._history.extend([{'role':'user','text':'Alte Recherche: Frankfurt nach Warschau.'},
        {'role':'assistant','text':'Die alte Recherche ist beendet; mehrere frühere Ergebnisse liegen vor.'}])
    return old


def old_unchanged(w, old):
    from dataclasses import asdict
    for task, run in old:
        require_equal(asdict(w.ledger.get_task(task['task_id'])), task)
        require_equal(asdict(w.ledger.get_run(run['run_id'])), run)
        require_equal(len(w.ledger.runs_for_task(task['task_id'])), 1)


def task_counts(w):
    with w.ledger._open() as db:
        return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('agent_tasks', 'agent_runs', 'agent_task_grants'))


async def t_original_new_research_after_chat_lookup_preserves_old_runs_and_admits_once():
    async with world(script=research_script()) as w:
        old = old_research(w)
        before = task_counts(w)
        await say(w, FLIGHT_REQUEST, 'fresh-flight-after-old-chat')
        await C.H.until(lambda:not w.session._research_watchers)
        require_equal(task_counts(w), tuple(n+1 for n in before))
        require_equal(len(w.native_launches), 2)
        first, second = [C.payload(prompt) for prompt in w.native_launches]
        require_equal((first['user_text'], second['user_text']), (FLIGHT_REQUEST, FLIGHT_REQUEST))
        require_equal(first['revision'], second['revision'])
        status = second['core_results'][0]
        require_equal(status['name'], 'agent_task_status')
        require(status['result']['success'])
        require(status['result']['data']['ambiguous'])
        require_equal(status['result']['data']['gesamt'], 3)
        # The same source row owns both cost operations and the task receipt.
        native = C.G.claims(w)
        require_equal(len(native), 2)
        require(all((r['phase'], r['state']) == ('voice_delegate', 'finished') for r in native))
        with w.ledger._open() as db:
            activities = list(db.execute('SELECT message_id,state FROM agent_cost_activities ORDER BY accepted_at'))
            money = list(db.execute('SELECT state,actual_cents FROM agent_cost_reservations'))
        require_equal(len(activities), 2)
        require_equal(activities[0]['message_id'], activities[1]['message_id'])
        require_equal([r['state'] for r in activities], ['completed', 'completed'])
        require_equal([tuple(r) for r in money], [('settled', 0), ('settled', 0)])
        admitted = w.session._tool_results[-1]
        require(admitted['success'], admitted)
        run = w.ledger.get_run(admitted['data']['run_id'])
        task = w.ledger.get_task(run.task_id)
        require_equal(task.objective, FLIGHT_REQUEST)
        require_equal(task.scope, S.SCOPE_RESEARCH)
        grant = w.orch.task_authority.for_run(run.run_id)
        require_equal(grant.receipt_method, 'dashboard_session')
        require(w.orch.task_authority.active(grant.reference, task_id=task.task_id, run_id=run.run_id).allowed)
        old_unchanged(w, old)
        require_equal(await w.store.list_pending(), [])
        stored = w.server.conversations.recent_context(w.session.conversation_id)
        require_equal(sum(r['role']=='user' and r['text']==FLIGHT_REQUEST for r in stored), 1)
        require(w.session._read_continuation is None)
        await notice(w, 'extra-flight-callback', 1000)
        require_equal(len(w.native_launches), 2)
        require_equal(task_counts(w), tuple(n+1 for n in before))
        await C.H.end(w, w.client)
        require_equal(S.AgentRunLedger(w.ledger.path).get_task(task.task_id).objective, FLIGHT_REQUEST)
        old_unchanged(w, old)


async def t_new_research_can_follow_missing_or_failed_chat_status_without_rewriting_it():
    for states in ((), (S.FAILED,)):
        async with world(script=research_script()) as w:
            old = old_research(w, states)
            before = task_counts(w)
            await say(w, FLIGHT_REQUEST, 'fresh-flight-without-completed-result')
            await C.H.until(lambda:not w.session._research_watchers)
            require_equal(task_counts(w), tuple(n+1 for n in before))
            require_equal(len(w.native_launches), 2)
            status = C.payload(w.native_launches[-1])['core_results'][0]['result']['data']
            if states:
                require_equal(status['auftrag']['zustand_code'], S.FAILED)
            else:
                require(not status['found']); require_equal(status['gesamt'], 0)
            old_unchanged(w, old)
            await C.H.end(w, w.client)


async def t_chat_reselection_refuses_unbound_goal_repeat_read_other_effects_and_ambiguous_revision():
    for mode in ('wrong_goal', 'repeat', 'personal', 'mail', 'mixed', 'observation', 'wrong_continue'):
        async with world(script=research_script(mode)) as w:
            old = old_research(w)
            before = task_counts(w)
            await say(w, FLIGHT_REQUEST, 'refuse-'+mode)
            require_equal(task_counts(w), before)
            require(w.session._read_continuation is None)
            require_equal(await w.store.list_pending(), [])
            old_unchanged(w, old)
            require_equal(len(w.native_launches), 2)
            require_equal(len(C.G.claims(w)), 2)
            require(all(r['state']=='finished' for r in C.G.claims(w)))
            await notice(w, 'no-third-'+mode, 1000)
            require_equal(len(w.native_launches), 2)
            await C.H.end(w, w.client)


async def t_chat_status_question_never_acquires_a_new_research_reselection():
    async with world(script=research_script()) as w:
        old = old_research(w)
        before = task_counts(w)
        await say(w, 'Wie weit ist mein letzter Rechercheauftrag?', 'read-only-current-chat')
        require(w.gate.context().commanded is False)
        require_equal(len(w.native_launches), 1)
        require_equal(task_counts(w), before)
        require(w.session._read_continuation is None)
        await notice(w, 'no-start-from-status', 1000)
        require_equal(len(w.native_launches), 1)
        old_unchanged(w, old)
        await C.H.end(w, w.client)


async def t_chat_research_reselection_rechecks_original_source_and_deadline_before_admission():
    for loss in ('correction', 'stored_text', 'foreign_context', 'revoke', 'close', 'deadline'):
        async with world(script=research_script(delay=.2)) as w:
            old = old_research(w)
            before = task_counts(w)
            if loss == 'deadline':
                backend = w.server.dispatcher.live_backend
                choose = backend.choose
                async def shorten_next(snapshot):
                    result = await choose(snapshot)
                    if not json.loads(json.loads(snapshot.input_json)[1]['content'])['core_results']:
                        backend.transport.timeout = .05
                    return result
                backend.choose = shorten_next
            changing = asyncio.create_task(say(w, FLIGHT_REQUEST, 'pending-new-flight'))
            await C.H.until(lambda:len(w.native_launches)==2, seconds=4)
            if loss == 'correction':
                await w.provider.events.put(C.H.transcript('Nein, noch nicht recherchieren.', 1000, 1500, 'correction'))
                await C.H.until(lambda:bool(w.session._input_parts))
            elif loss == 'stored_text':
                row = w.server.conversations.conn.execute(
                    'SELECT message_id FROM conversation_messages WHERE conversation_id=? '
                    'AND source_session_id=? AND role=? AND text=?',
                    (w.session.conversation_id, w.session.session_id, 'user', FLIGHT_REQUEST)).fetchone()
                require(row is not None)
                w.server.conversations.conn.execute('UPDATE conversation_messages SET text=? WHERE message_id=?',
                    ('Anderer künstlicher Originaltext.', row[0]))
            elif loss == 'foreign_context':
                w.gate._context = replace(w.gate.context(), principal='foreign-owner')
            elif loss == 'revoke':
                await w.service.revoke(w.auth['session_id'], principal='local-owner')
            elif loss == 'close':
                await C.H.end(w, w.client)
            await changing
            require_equal(task_counts(w), before)
            require(w.session._read_continuation is None)
            old_unchanged(w, old)
            require_equal(len(w.native_launches), 2)
            if loss == 'deadline':
                require_equal([r['state'] for r in C.G.claims(w)], ['finished', 'unknown'])
                require(any('zu lange' in json.dumps(row, ensure_ascii=False) for row in w.provider.sent))
            if loss != 'close' and w.session.active:
                await C.H.end(w, w.client)


async def t_direct_new_research_keeps_existing_authority_path_with_old_chat_results():
    async with world(script=research_script('direct')) as w:
        old = old_research(w)
        before = task_counts(w)
        await say(w, FLIGHT_REQUEST, 'direct-new-flight')
        await C.H.until(lambda:not w.session._research_watchers)
        require_equal(task_counts(w), tuple(n+1 for n in before))
        require_equal(len(w.native_launches), 1)
        require_equal(C.payload(w.native_launches[0])['core_results'], [])
        require_equal(w.ledger.get_task(w.session._tool_results[-1]['data']['task_id']).objective, FLIGHT_REQUEST)
        old_unchanged(w, old)
        await C.H.end(w, w.client)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
