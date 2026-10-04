"""N6: actual early native events, authenticated replay and bounded pipes."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_public_research import world as research_world
from test_agent_task_entry import world as entry_world
from test_hermes_native import native_fixture, scoped, method_rows, FREE
from solvio.agent_runtime import store as S, cost_dispatch as D, costs as C
from solvio.agent_runtime.progress import NativeProgress
from solvio.specialists import hermes_native as N, launcher as L


async def t_https_observes_real_worker_before_completion_and_replays_without_dispatch():
    async with research_world('live_pause') as w:
        accepted, _ = await w.admit()
        run_id = accepted['run_id']
        path = '/v1/agent/runs/'+run_id
        work = asyncio.create_task(w.tick_until(run_id, S.SUCCEEDED))
        try:
            page = None
            async with asyncio.timeout(12):
                while True:
                    response = await w.client.get(path+'/events')
                    require_equal(response.status, 200)
                    page = await response.json()
                    native = [e for e in page['events'] if e['kind']=='native_progress']
                    if len(native) >= 2:
                        break
                    require(not work.done(), 'worker finished before live observation')
                    await asyncio.sleep(.02)
            require(not work.done())
            detail = await (await w.client.get(path)).json()
            require_equal(detail['zustand_code'], S.WAITING_SPECIALIST)
            require_equal([e['observation']['seq'] for e in native], [1, 2])
            claims = D.invocations(w.ledger, accepted['task_id'])
            claim = next(c for c in claims if c['phase']=='specialist')
            require_equal(claim['state'], 'claimed')
            for event in native:
                require_equal(event['observation']['invocation_id'], claim['invocation_id'])
                step = w.ledger.get_step(event['step_id'])
                require_equal(step.run_id, run_id)
                require_equal(step.state, 'running')
                require_equal(event['observation']['native_thread_id'], 'local-thread')
                require('query' not in event['observation'])
            other = await w.new_client(); await w.login(other, 'someone-else')
            require_equal((await other.get(path+'/events')).status, 404)
            before = len(method_rows(w.rpc, 'turn/start'))
            again = await (await w.client.get(path+'/events')).json()
            require_equal(again, page)
            require_equal(len(method_rows(w.rpc, 'turn/start')), before)
        finally:
            (w.rpc/'release-live').write_text('continue local test')
            await asyncio.wait_for(work, 12)
        require_equal(w.ledger.get_run(run_id).state, S.SUCCEEDED)
        await w.detach()
        w.runtime()
        reader, _ = await w.fresh_reader()
        replay = await (await reader.get(path+'/events')).json()
        require_equal([e for e in replay['events'] if e['kind']=='native_progress'], native)
        require_equal(len(method_rows(w.rpc, 'turn/start')), 1)


def event(kind='started', **changes):
    return dict(schema=2, type='event', event=kind, seq=1,
                runtime=N.RUNTIME, model='test-model', thread_id='thread-a', turn_id='turn-a', **changes)


def t_decoder_rejects_foreign_replayed_old_or_trailing_events():
    first = event()
    second = dict(schema=2, type='event', event='web_search', seq=2,
                  thread_id='thread-a', turn_id='turn-a', status='started', item_id='search-a')
    for bad in [dict(second, seq=1), dict(second, thread_id='foreign'),
                dict(second, turn_id='foreign'), dict(second, schema=1),
                dict(second, command='untrusted'), dict(second, query='x'*351),
                dict(second, event='browser_read', query='untrusted-page-argument')]:
        seen=[]; decoder=N.NativeDecoder(L.redact, seen.append)
        decoder.feed(json.dumps(first))
        try:
            decoder.feed(json.dumps(bad))
        except ValueError:
            pass
        else:
            raise AssertionError('invalid live event accepted')
        require_equal(len(seen), 1)
    decoder=N.NativeDecoder(L.redact)
    decoder.feed(json.dumps(first))
    decoder.feed(json.dumps(dict(schema=2,type='result',thread_id='thread-a',turn_id='turn-a')))
    try:
        decoder.feed(json.dumps(second))
    except ValueError:
        pass
    else:
        raise AssertionError('trailing event after result accepted')


async def t_native_progress_cannot_write_without_live_claim_or_after_step_cancellation():
    with native_fixture() as (_, _, ledger, task, run, request):
        step=ledger.create_step(run_id=run,seq=1,kind='specialist',specialist_profile=request.profile)
        ledger.update_step(step.step_id,state='running',started=True)
        ledger.transition(run,S.WAITING_SPECIALIST)
        sink=NativeProgress(ledger,run,step.step_id)
        with scoped(ledger,task,run):
            sink(event())
            require_equal([e for e in ledger.events_for_run(run) if e.kind=='native_progress'], [])
            async def local(invocation,prompt):
                sink(event())
                sink(event())
                ledger.update_step(step.step_id,state='failed')
                sink(dict(event(), seq=2, event='web_search', status='completed', item_id='item-a'))
                return L.Outcome(True,exit_code=0)
            await D.dispatch('codex',L.Invocation(sys.executable,(),timeout=5),'',local)
            sink(dict(event(),seq=2,event='web_search',status='completed',item_id='item-a'))
        events=[e for e in ledger.events_for_run(run) if e.kind=='native_progress']
        require_equal(len(events),1)
        require_equal(json.loads(events[0].ref)['seq'],1)


async def t_browser_observations_survive_worker_decoder_and_bound_ledger_without_page_content():
    import contextlib
    import io
    from solvio.specialists import hermes_native_worker as W, hermes_browser as B
    with native_fixture() as (_, _, ledger, task, run, request):
        step = ledger.create_step(run_id=run, seq=1, kind='specialist', specialist_profile=request.profile)
        ledger.update_step(step.step_id, state='running', started=True)
        ledger.transition(run, S.WAITING_SPECIALIST)
        sink = NativeProgress(ledger, run, step.step_id)
        recorder = W.Recorder()
        recorder.thread_id, recorder.turn_id = 'thread-a', 'turn-a'
        recorder.browser_tools = B.TOOLS
        stream = io.StringIO()
        def note(method, tool='browser_navigate', thread='thread-a'):
            return {'method': method, 'params': {'threadId': thread, 'turnId': 'turn-a',
                'item': {'id': 'browser-a', 'type': 'mcpToolCall', 'server': B.SERVER,
                         'tool': tool, 'arguments': {'url': 'https://example.org/private-query'},
                         'result': {'text': 'PAGE-CONTENT-MUST-NOT-BE-JOURNALLED'}}}}
        with contextlib.redirect_stdout(stream):
            recorder.emit('started', runtime=N.RUNTIME, model='test-model')
            recorder.on_event(note('item/started'))
            recorder.on_event(note('item/completed'))
            recorder.on_event(note('item/completed', thread='foreign'))
        decoder = N.NativeDecoder(L.redact, sink)
        with scoped(ledger, task, run):
            async def local(invocation, prompt):
                for line in stream.getvalue().splitlines(): decoder.feed(line)
                return L.Outcome(True, exit_code=0)
            await D.dispatch('codex', L.Invocation(sys.executable, (), timeout=5), '', local)
        observed = [e for e in ledger.events_for_run(run) if e.kind == 'native_progress']
        require_equal([json.loads(e.ref)['event'] for e in observed],
                      ['started', 'browser_read', 'browser_read'])
        require_equal([json.loads(e.ref)['status'] for e in observed], ['', 'started', 'completed'])
        require('PAGE-CONTENT' not in stream.getvalue() and 'private-query' not in stream.getvalue())
        require(all('PAGE-CONTENT' not in e.ref and 'private-query' not in e.ref for e in observed))
        before = recorder.events
        with contextlib.redirect_stdout(io.StringIO()):
            recorder.on_event(note('item/completed', tool='browser_click'))
        require_equal(recorder.failure, 'native_tool_not_allowed')
        require_equal(recorder.events, before)


async def t_running_task_worker_progress_requires_exact_outer_step_and_active_claim():
    # A real ledger and cost dispatch, but no native/model process is needed to
    # exercise this synchronous journal boundary under its actual ContextVar.
    cases = ('bound', 'research', 'build', 'profile', 'kind', 'operation',
             'foreign_run', 'not_started', 'finished')
    for case in cases:
        with tempfile.TemporaryDirectory() as folder:
            ledger = S.AgentRunLedger(str(Path(folder) / 'agent.sqlite3'))
            task = ledger.create_task(objective='Prüfe den lokalen Auftrag mit Quellen.',
                scope=case if case in {'research', 'build'} else 'task',
                created_origin='trusted_interactive_app', created_principal='owner:device')
            run = ledger.create_run(task_id=task.task_id).run_id
            ledger.transition(run, S.PLANNING); ledger.transition(run, S.RUNNING)
            C.CostLedger(ledger).configure(task.task_id)
            step = ledger.create_step(run_id=run, seq=1,
                kind='capability' if case == 'kind' else 'specialist',
                specialist_profile='researcher/hermes' if case == 'profile' else 'worker/codex')
            ledger.update_step(step.step_id, state='running', started=case != 'not_started')
            if case == 'finished':
                with ledger._open() as db:
                    db.execute('UPDATE agent_steps SET finished_at=1 WHERE step_id=?', (step.step_id,))
            observed_run = ledger.create_run(task_id=task.task_id).run_id if case == 'foreign_run' else run
            sink = NativeProgress(ledger, observed_run, step.step_id)
            operation = 'unrelated-operation' if case == 'operation' else step.step_id
            with D.task_cost_scope(ledger, task_id=task.task_id, run_id=run, phase='specialist',
                    operation_id=operation, quote_adapter=lambda *_: FREE):
                sink(event())  # Scope without a physical claim grants nothing.
                require_equal([e for e in ledger.events_for_run(run) if e.kind == 'native_progress'], [])
                async def local(invocation, prompt):
                    sink(event()); sink(event())
                    if case == 'bound':
                        sink(dict(event(), seq=2, event='web_search', status='completed', item_id='item-a'))
                        # A callback received after cancellation cannot journal
                        # additional progress even while its dispatch is alive.
                        ledger.transition(run, S.CANCELLED)
                        sink(dict(event(), seq=3, event='web_search', status='completed', item_id='item-b'))
                    return L.Outcome(True, exit_code=0)
                await D.dispatch('codex', L.Invocation(sys.executable, (), timeout=5), '', local)
                sink(dict(event(), seq=4, event='web_search', status='completed', item_id='item-c'))
            observed = [e for e in ledger.events_for_run(run) if e.kind == 'native_progress']
            require_equal(len(observed), 2 if case == 'bound' else 0, case)
            if case == 'bound':
                claim = D.invocations(ledger, task.task_id)[0]
                require_equal([json.loads(e.ref)['seq'] for e in observed], [1, 2])
                require_equal({json.loads(e.ref)['invocation_id'] for e in observed}, {claim['invocation_id']})
                require_equal({json.loads(e.ref)['operation_id'] for e in observed}, {step.step_id})
                require_equal(observed[0].summary, 'Hermes hat den nativen Auftrag gestartet.')


async def t_bounded_pipe_delivers_complete_utf8_before_exit_and_drains_overflow():
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder); release=root/'release'
        source=root/'local.py'
        source.write_text("import pathlib,sys,time\n"
            "data='Grüße\\n'.encode()\n"
            "for b in data: sys.stdout.buffer.write(bytes([b]));sys.stdout.buffer.flush()\n"
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\n"
            "sys.stderr.buffer.write(b'x'*600000)\n"
            "sys.stdout.buffer.write(b'x'*600000)\n")
        lines=[]
        def received(line):
            lines.append(line)
            release.write_text('received while process waits')
        outcome=await L.run(L.Invocation(sys.executable,(str(source),),cwd=folder,timeout=5),
                            '',on_stdout_line=received)
        require_equal(lines,['Grüße'])
        require(outcome.truncated)
        require(len(outcome.text)<=L.MAX_OUTPUT)
        require(len(outcome.stderr_note)<=400)
        require_equal(outcome.exit_code,0)


async def t_cursor_is_read_only_owner_scoped_bounded_and_reopens_existing_ledger():
    async with entry_world() as w:
        accepted=await (await w.start()).json();run=accepted['run_id']
        for i in range(6):
            w.ledger.record_event(run,'recovered',f'Beleg {i}')
        path='/v1/agent/runs/'+run+'/events'
        initial=w.ledger.events_for_run(run)
        result=[];cursor=0
        for _ in range(10):
            response=await w.client.get(path+f'?after={cursor}&limit=2')
            require_equal(response.status,200)
            page=await response.json();result.extend(page['events']);cursor=page['next_cursor']
            if not page['has_more']:break
        require_equal([e['id'] for e in result],[e.id for e in initial])
        for query in ('after=-1','after=no','limit=101','limit=0'):
            require_equal((await w.client.get(path+'?'+query)).status,400)
        require_equal(w.ledger.events_for_run(run),initial)
        reopened=S.AgentRunLedger(w.ledger.path)
        require_equal([e.id for e in reopened.events_after(run,0,100)['events']], [e.id for e in initial])


async def t_public_cancel_interrupts_and_drains_actual_native_process_before_confirmation():
    import os
    async with research_world('live_cancel') as w:
        accepted,_=await w.admit();run=accepted['run_id']
        async def drive():
            while not w.ledger.get_run(run).terminal:
                await w.orch.tick()
                await asyncio.sleep(.01)
        work=asyncio.create_task(drive())
        try:
            async with asyncio.timeout(12):
                while len([e for e in w.ledger.events_for_run(run) if e.kind=='native_progress'])<2:
                    await asyncio.sleep(.02)
            child=int((w.rpc/'child.pid').read_text())
            result=await w.client.post('/v1/agent/runs/'+run+'/cancel',json={},headers=w.headers)
            require_equal(result.status,200)
            require_equal(w.ledger.get_run(run).state,S.CANCELLED)
            interrupts=method_rows(w.rpc,'turn/interrupt')
            require(interrupts, 'no native interruption requested')
            require(all(r['params']=={'threadId':'local-thread','turnId':'local-turn'} for r in interrupts))
            claim=next(c for c in D.invocations(w.ledger,accepted['task_id']) if c['phase']=='specialist')
            require_equal(claim['state'],'unknown')
            require('ungeklärt' in w.ledger.get_run(run).result_summary)
            require_equal([s.state for s in w.ledger.steps_for_run(run) if s.kind=='specialist'],['unknown'])
            from test_hermes_native import observed
            native_pids=[r['pid'] for r in observed(w.rpc) if r.get('argv') and r['argv'][0]=='app-server']
            for pid in native_pids+[child]:
                for _ in range(100):
                    try: os.kill(pid,0)
                    except ProcessLookupError: break
                    await asyncio.sleep(.02)  # allow init to reap a killed orphan
                else: raise AssertionError('native process survived public cancellation')
            require_equal(len(method_rows(w.rpc,'turn/start')),1)
            await asyncio.wait_for(work,3)
            require_equal((await w.client.post('/v1/agent/runs/'+run+'/resume',json={},headers=w.headers)).status,409)
        finally:
            if not work.done():
                work.cancel()
            await asyncio.gather(work,return_exceptions=True)


async def t_owner_cancel_and_runtime_shutdown_both_wait_for_real_process_cleanup():
    import os
    from unittest.mock import patch
    from solvio.agent_runtime import orchestrator as O
    from test_hermes_native import observed
    async with research_world('live_cancel') as w:
        accepted,_=await w.admit();run=accepted['run_id']
        entered=asyncio.Event();release=asyncio.Event();original=L._stop_process
        processes=[];cancel=shutdown=None
        async def held(process, grace=0):
            processes.append(process);entered.set();await release.wait()
            await original(process, grace)
        with patch.object(O,'TICK_SECONDS',.01), patch.object(L,'_stop_process',held):
            await w.orch.start()
            try:
                async with asyncio.timeout(12):
                    while len([e for e in w.ledger.events_for_run(run) if e.kind=='native_progress'])<2:
                        await asyncio.sleep(.02)
                active=w.orch._advances[run]
                child=int((w.rpc/'child.pid').read_text())
                cancel=asyncio.create_task(w.client.post('/v1/agent/runs/'+run+'/cancel',json={},headers=w.headers))
                await asyncio.wait_for(entered.wait(),3)
                shutdown=asyncio.create_task(w.orch.stop())
                async with asyncio.timeout(3):
                    while active.cancelling()<2:await asyncio.sleep(.01)
                require(not cancel.done());require(not shutdown.done())
                require(not w.ledger.get_run(run).terminal)
                release.set()
                require_equal((await asyncio.wait_for(cancel,10)).status,200)
                await asyncio.wait_for(shutdown,10)
                require_equal(w.ledger.get_run(run).state,S.CANCELLED)
                require_equal(next(c for c in D.invocations(w.ledger,accepted['task_id']) if c['phase']=='specialist')['state'],'unknown')
                require_equal([s.state for s in w.ledger.steps_for_run(run) if s.kind=='specialist'],['unknown'])
                interrupts=method_rows(w.rpc,'turn/interrupt')
                require(interrupts)
                require(all(r['params']=={'threadId':'local-thread','turnId':'local-turn'} for r in interrupts))
                require_equal(len(method_rows(w.rpc,'turn/start')),1)
                pids={child,*(p.pid for p in processes),*(r['pid'] for r in observed(w.rpc) if r.get('argv') and r['argv'][0]=='app-server')}
                for pid in pids:
                    for _ in range(100):
                        try:os.kill(pid,0)
                        except ProcessLookupError:break
                        await asyncio.sleep(.02)
                    else:raise AssertionError('process survived concurrent cancellation')
            finally:
                release.set()
                for process in processes:await original(process,0)
                if w.orch._task is not None:await w.orch.stop()
                await asyncio.gather(*(task for task in (cancel,shutdown) if task),return_exceptions=True)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
