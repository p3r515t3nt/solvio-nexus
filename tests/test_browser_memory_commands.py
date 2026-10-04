"""N5 commands cross real HTTPS, router, S1, journal and temporary memory."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_nexus_dashboard import world, record
from solvio.capabilities.browser_memory_command import BrowserMemoryCommand
from solvio.capabilities.policy import OriginClass
from solvio.capabilities.envelope import CapabilityOutcome as O
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.security.mobile_approval import store as S, execution as X
from solvio.memory.adaptive import candidates as C

PATH='/v1/dashboard/memory-commands'


def body(mid, *, key='memory-command-1', statement='Ich bevorzuge jetzt ruhige Zimmer.'):
    return {'capability':'memory_correct','arguments':{'memory_id':mid,'statement':statement},'client_request_id':key}


async def t_owner_corrects_once_and_reads_same_command_after_a_fresh_login():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('Ich bevorzuge Zimmer am Aufzug.'))
        request=body(mid)
        replies=await asyncio.gather(*(w.client.post(PATH,json=request,headers=w.headers) for _ in range(4)))
        data=await asyncio.gather(*(r.json() for r in replies))
        keys={r['command_id'] for r in data};require_equal(len(keys),1)
        command_id=keys.pop()
        final=await (await w.client.get(PATH+'/'+command_id)).json()
        require_equal(final['state'],'succeeded')
        require_equal(final['attempt_states'],[X.SUCCEEDED])
        current=await w.memory.semantic.memory.active_records()
        require_equal(len(current),1)
        require_equal(current[0].content,request['arguments']['statement'])
        require_equal(current[0].supersedes,mid)
        require_equal(current[0].source,'correction:dashboard')
        req=await w.store.get_request(command_id)
        require_equal(req['decision_method'],S.MEMORY_COMMAND_METHOD)
        require_equal(req['decided_device'],None)
        receipt=await w.store.task_authorization_receipt(command_id,w.cp.core_instance_id)
        require_equal(receipt[0],None)
        second=await w.new_client();headers=await w.login(second)
        replay=await (await second.post(PATH,json=request,headers=headers)).json()
        require_equal(replay['state'],'succeeded')
        require_equal(len(await w.memory.semantic.memory.active_records()),1)
        require_equal((await second.get(PATH+'/'+command_id)).status,200)


async def t_same_command_id_with_changed_content_is_a_conflict_without_second_write():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('Alte Vorliebe.'))
        require_equal((await w.client.post(PATH,json=body(mid),headers=w.headers)).status,200)
        changed=await w.client.post(PATH,json=body(mid,statement='Andere Korrektur.'),headers=w.headers)
        require_equal(changed.status,409)
        require_equal((await changed.json())['error'],'memory_command_conflict')
        require_equal(len(await w.memory.semantic.memory.active_records()),1)


async def t_two_tabs_cannot_create_two_active_corrections_of_the_same_old_memory():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('Alte Zimmerpräferenz.'))
        replies=await asyncio.gather(*(w.client.post(PATH,json=body(mid,key=key,statement=statement),headers=w.headers)
            for key,statement in [('view-a','Ruhige Zimmer.'),('view-b','Zimmer mit Balkon.')]))
        data=await asyncio.gather(*(r.json() for r in replies))
        require_equal(sorted(r['state'] for r in data),['not_executed','succeeded'])
        failed=next(r for r in data if r['state']=='not_executed')
        require_equal(failed['reason'],'memory_target_changed')
        require_equal(failed['attempt_states'],[X.FAILED_SAFE])
        require_equal(len(await w.memory.semantic.memory.active_records()),1)


async def t_owner_cookie_without_csrf_foreign_owner_and_forged_body_cannot_write():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('Meine aktuelle Vorliebe.'))
        require_equal((await w.client.post(PATH,json=body(mid))).status,401)
        foreign=await w.new_client();headers=await w.login(foreign,'not-owner')
        require_equal((await foreign.post(PATH,json=body(mid),headers=headers)).status,401)
        require_equal((await w.client.post(PATH,json=dict(body(mid),principal='local-owner'),headers=w.headers)).status,400)
        require_equal((await w.client.post(PATH,json={**body(mid),'capability':'note_write'},headers=w.headers)).status,400)
        require_equal((await w.memory.semantic.get(mid)).content,'Meine aktuelle Vorliebe.')


async def t_partial_write_or_false_handler_is_never_success_or_repeated():
    for failure in ('false','after_write'):
        async with world() as w:
            mid=await w.memory.semantic.remember(record('Vorher.'))
            original=w.router._handlers['memory_correct'];calls=[]
            async def handler(arguments):
                calls.append(1)
                if failure=='false': return {'ok':False,'reason':'synthetic'}
                await original(arguments)
                raise RuntimeError('synthetic after canonical commit')
            w.router._handlers['memory_correct']=handler
            first=await (await w.client.post(PATH,json=body(mid),headers=w.headers)).json()
            require_equal(first['state'],'outcome_unconfirmed')
            require_equal(first['attempt_states'],[X.UNKNOWN])
            again=await (await w.client.post(PATH,json=body(mid),headers=w.headers)).json()
            require_equal(again['state'],'outcome_unconfirmed')
            require_equal(len(calls),1)
            require_equal(len(await w.memory.semantic.memory.active_records()),1)


async def t_revocation_after_claim_stops_before_handler_and_command_cannot_mint_task_scope():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('Nicht verändern.'))
        original=w.store.begin_external_execution
        async def revoke_then_begin(*,attempt_id,identities):
            require(type(identities) is S.BrowserCommandAuthorization)
            await w.sessions.revoke(identities.session_id,principal='local-owner')
            return await original(attempt_id=attempt_id,identities=identities)
        with patch.object(w.store,'begin_external_execution',revoke_then_begin):
            response=await w.client.post(PATH,json=body(mid),headers=w.headers)
        data=await response.json()
        require_equal(data['state'],'not_executed')
        require_equal(data['attempt_states'],[X.ABANDONED])
        require_equal((await w.memory.semantic.get(mid)).content,'Nicht verändern.')


async def candidate(w):
    value=await w.adaptive.candidates.observe(C.Candidate(id='',statement='Ich mag ruhige Hotels.',
        dedup_key='candidate-quiet-hotels',kind='inferred',memory_type='preference',subject='owner',sensitivity='personal'),
        C.Evidence(at=C.utcnow().isoformat(),kind='observed',conversation_id='fixture',message_id='fixture-1'))
    return await w.adaptive.candidates.transition(value.id,C.ASK_PENDING)


async def t_parallel_candidate_confirmation_or_denial_has_one_durable_winner():
    for other_action in ('memory_confirm_candidate','memory_decline_candidate'):
        async with world() as w:
            cand=await candidate(w)
            # Both old views are read before either gets its durable claim.
            original=w.adaptive.candidates.get;barrier=asyncio.Event();count=0
            async def simultaneous_get(cid):
                nonlocal count
                result=await original(cid);count+=1
                if count==2:barrier.set()
                await asyncio.wait_for(barrier.wait(),2)
                return result
            with patch.object(w.adaptive.candidates,'get',simultaneous_get):
                replies=await asyncio.gather(*(w.client.post(PATH,headers=w.headers,json={
                    'capability':action,'arguments':{'candidate_id':cand.id},'client_request_id':'tab-'+str(index)})
                    for index,action in enumerate(('memory_confirm_candidate',other_action))))
            data=await asyncio.gather(*(r.json() for r in replies))
            require_equal(sum(row['state']=='succeeded' for row in data),1)
            latest=await original(cand.id)
            active=await w.memory.semantic.memory.active_records()
            require_equal(len(active),0 if latest.state==C.DECLINED else 1)
            if active:
                require_equal(active[0].source,'confirmation:dashboard')
                require_equal(active[0].metadata['candidate_id'],cand.id)


async def t_claimed_candidate_failure_is_visible_and_cannot_write_again():
    async with world() as w:
        cand=await candidate(w);original=w.memory.semantic.remember;calls=[]
        async def after_commit(record):
            calls.append(1);await original(record);raise RuntimeError('synthetic post-commit loss')
        request={'capability':'memory_confirm_candidate','arguments':{'candidate_id':cand.id},'client_request_id':'first'}
        with patch.object(w.memory.semantic,'remember',after_commit):
            data=await (await w.client.post(PATH,json=request,headers=w.headers)).json()
        require_equal(data['state'],'outcome_unconfirmed')
        require_equal((await w.adaptive.candidates.get(cand.id)).state,C.ADOPTING)
        listed=await (await w.client.get('/v1/memory/candidates')).json()
        require_equal(listed['candidates'][0]['state'],C.ADOPTING)
        again=await (await w.client.post(PATH,json={**request,'client_request_id':'another-tab'},headers=w.headers)).json()
        require(again['state']!='succeeded')
        require_equal(len(await w.memory.semantic.memory.active_records()),1)
        require_equal(len(calls),1)


async def t_preclaim_revocation_is_not_reported_as_waiting():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('So bleibt es.'))
        original=w.co.execute_approved
        async def revoke(approval_id,executor):
            req=await w.store.get_request(approval_id)
            await w.sessions.revoke(req['decided_session'],principal='local-owner')
            return await original(approval_id,executor)
        with patch.object(w.co,'execute_approved',revoke):
            data=await (await w.client.post(PATH,json=body(mid),headers=w.headers)).json()
        require_equal(data['state'],'not_executed')
        require_equal(data['reason'],'browser_session_revoked')
        require_equal(data['attempt_states'],[])
        other=await w.new_client();await w.login(other)
        latest=await (await other.get(PATH+'/'+data['command_id'])).json()
        require_equal(latest['state'],'not_executed')
        require_equal((await w.memory.semantic.get(mid)).content,'So bleibt es.')


async def t_status_reads_one_snapshot_while_another_connection_completes_the_command():
    async with world() as w:
        mid = await w.memory.semantic.remember(record('Vorher.'))
        request = body(mid)
        async def parked(*a, **kw): return None
        with patch.object(w.co, 'execute_approved', parked):
            prepared = await (await w.client.post(PATH, json=request, headers=w.headers)).json()
        require_equal(prepared['state'], 'pending')
        reader = S.ApprovalControlStore(w.store.path, read_only=True)
        await reader.open()
        loop = asyncio.get_running_loop(); finished = threading.Event(); triggered = []
        original_authority = reader._browser_command_authorization
        original_finish = w.store.finish_execution_attempt
        async def finishing(**kwargs):
            result = await original_finish(**kwargs)
            finished.set()
            return result
        async def post():
            return await (await w.client.post(PATH, json=request, headers=w.headers)).json()
        def authority(*args):
            if not triggered:
                triggered.append(asyncio.run_coroutine_threadsafe(post(), loop))
                require(finished.wait(5), 'concurrent canonical command did not finish')
            return original_authority(*args)
        try:
            # The GET's actual status reader has a separate read-only WAL
            # connection. The real POST commits through the normal writer.
            with patch.object(w.store, 'browser_memory_status_snapshot', reader.browser_memory_status_snapshot), \
                 patch.object(reader, '_browser_command_authorization', authority), \
                 patch.object(w.store, 'finish_execution_attempt', finishing):
                data = await (await w.client.get(PATH+'/'+prepared['command_id'])).json()
                posted = await asyncio.wait_for(asyncio.wrap_future(triggered[0]), 5)
            require_equal(data['state'], 'pending')
            require_equal(data['request_state'], S.APPROVED)
            require_equal(data['attempt_states'], [])
            require_equal(posted['state'], 'succeeded')
            require_equal((await (await w.client.get(PATH+'/'+prepared['command_id'])).json())['state'], 'succeeded')
            require_equal(len(await w.memory.semantic.memory.active_records()), 1)
        finally:
            await reader.close()


async def t_historical_success_requires_the_actual_bound_claim_even_after_logout():
    async with world() as w:
        mid = await w.memory.semantic.remember(record('Vorher.'))
        data = await (await w.client.post(PATH, json=body(mid), headers=w.headers)).json()
        req = await w.store.get_request(data['command_id'])
        await w.sessions.revoke(req['decided_session'], principal='local-owner')
        client = await w.new_client(); await w.login(client)
        require_equal((await (await client.get(PATH+'/'+data['command_id'])).json())['state'], 'succeeded')
        # Isolated corruption probe: a status label alone is no success proof.
        await w.store._run(lambda: w.store._conn.execute(
            'UPDATE execution_attempts SET claim_action_digest=? WHERE approval_id=?', ('wrong',data['command_id'])))
        require_equal((await (await client.get(PATH+'/'+data['command_id'])).json())['state'], 'outcome_unconfirmed')


async def t_router_rejects_forged_command_arguments_origin_and_version_before_journal():
    async with world() as w:
        mid=await w.memory.semantic.remember(record('Unverändert.'))
        session=await (await w.client.get('/v1/browser/session')).json()
        from solvio.security.mobile_approval.browser_sessions import BrowserActor
        actor=BrowserActor('local-owner',session['session_id'])
        arguments=body(mid)['arguments']
        proof=BrowserMemoryCommand.bind(actor,'binding-test','memory_correct',arguments)
        for changed,origin,token in [({**arguments,'statement':'Fälschung'},OriginClass.TRUSTED_DASHBOARD,proof),
                                     (arguments,OriginClass.ROOM_VOICE,proof),
                                     (arguments,OriginClass.TRUSTED_DASHBOARD,{'actor':actor})]:
            result=await w.router.execute('memory_correct',changed,
                trust=TrustContext(TrustLevel.USER_DIRECT,user_authorized=True),principal='local-owner',origin=origin,browser_command=token)
            require_equal(result.outcome,O.REJECTED_BY_POLICY)
        w.router._specs['memory_correct']=replace(w.router.spec('memory_correct'),version=2)
        result=await w.router.execute('memory_correct',arguments,
            trust=TrustContext(TrustLevel.USER_DIRECT,user_authorized=True),principal='local-owner',
            origin=OriginClass.TRUSTED_DASHBOARD,browser_command=proof)
        require_equal(result.outcome,O.REJECTED_BY_POLICY)
        require_equal(await w.store.recent_browser_memory_commands('local-owner',w.cp.core_instance_id),[])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
