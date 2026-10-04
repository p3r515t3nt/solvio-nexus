"""Personal native worker uses the canonical, owner-filtered task ledger."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_core_tools as C
from solvio.agent_runtime import task_overview as O, store as S, native_tools as N
from solvio.agent_runtime.task_start_service import TaskStartService
from solvio.specialists.native_task_profile import web_search_mode

BODY=dict(C.BODY,tool=O.NAME)


def task(w,text='Offener Fixtureauftrag',owner='owner:device'):
    t=w.ledger.create_task(objective=text,scope='task',created_principal=owner,
                           created_origin='trusted_interactive_app')
    return t,w.ledger.create_run(task_id=t.task_id)


async def t_native_overview_reads_only_owners_open_work_with_one_settled_local_claim():
    with C.tools_world(names=(O.NAME,),register=False) as w:
        O.register(w.router,w.ledger)
        own,run=task(w)
        task(w,'Fremder nicht sichtbarer Auftrag','owner:other')
        before=len(w.ledger.recent_runs())
        async def native():
            reply=await w.tools.call(BODY)
            require(reply['success'],C.payload(reply))
            data=C.payload(reply)['data']
            require_equal(data['offen_gesamt'],1)
            require_equal(data['auftraege'][0]['auftrag'],own.task_id)
            require_equal(data['auftraege'][0]['zustand'],'CREATED')
            require('Fremder' not in json.dumps(data))
            require_equal(await w.tools.call(BODY),reply)
            claims=[c for c in C.D.invocations(w.ledger,w.task) if c['provider']==O.ROUTE[0]]
            require_equal(len(claims),1);require_equal(claims[0]['state'],'finished')
        await C.dispatch(w,native)
        require_equal(len(w.ledger.recent_runs()),before)
        require_equal(w.ledger.get_run(run.run_id).state,'CREATED')
        require_equal(w.costs.view(w.task)['ai_tool']['spent_cents'],0)


def t_only_personal_new_tasks_get_the_overview_and_web_stays_disabled():
    with C.tools_world(names=(),register=False) as w:
        O.register(w.router,w.ledger)
        starts=TaskStartService.__new__(TaskStartService);starts.router=w.router
        for scope,private in [('task',False),('research',True),('build',True)]:
            require(O.NAME not in {g.name for g in starts.capabilities_for(scope,private_data=private)})
        require(O.NAME in {g.name for g in starts.capabilities_for('task',private_data=True)})
        require_equal(web_search_mode([O.NAME]),'disabled')
        require_equal(w.tools.manifest(),[])


def t_owner_filter_precedes_cap_and_waiting_work_precedes_other_open_work():
    with C.tools_world(names=(O.NAME,),register=False) as w:
        for i in range(O.LIMIT+3):task(w,'Eigener Auftrag '+str(i))
        _,waiting=task(w,'Wartet auf mich')
        w.ledger.transition(waiting.run_id,S.PLANNING)
        w.ledger.transition(waiting.run_id,S.RUNNING)
        w.ledger.transition(waiting.run_id,S.WAITING_USER)
        for i in range(40):task(w,'Fremder Auftrag '+str(i),'other:owner')
        data=O.overview(w.ledger,w.task,w.run)
        require_equal(data['offen_gesamt'],O.LIMIT+4)
        require(data['gekuerzt'])
        require_equal(len(data['auftraege']),O.LIMIT)
        require_equal(data['auftraege'][0]['lauf'],waiting.run_id)
        require('Fremder' not in json.dumps(data))


def t_completed_newer_run_hides_older_open_revision_and_raw_material_is_filtered():
    with C.tools_world(names=(O.NAME,),register=False) as w:
        t,old=task(w,'Alter Auftrag')
        new=w.ledger.create_run(task_id=t.task_id)
        # A restored ledger can retain older open rows. Read exactly the latest
        # revision, never resurrect it or change either status during a report.
        with w.ledger._open() as db:
            db.execute("UPDATE agent_runs SET state='SUCCEEDED' WHERE run_id=?",(new.run_id,))
        t,run=task(w,'Ein weiterer Auftrag')
        with w.ledger._open() as db:
            db.execute('UPDATE agent_tasks SET objective=? WHERE task_id=?',
                       ('Dein Einmalcode lautet: 123456',t.task_id))
        data=O.overview(w.ledger,w.task,w.run)
        require_equal(data['offen_gesamt'],1)
        require('123456' not in json.dumps(data))
        require_equal(w.ledger.get_run(old.run_id).state,'CREATED')


async def t_caller_cannot_choose_owner_or_borrow_overview_without_a_grant():
    with C.tools_world(names=(),register=False) as w:
        O.register(w.router,w.ledger)
        task(w)
        async def native():
            reply=await w.tools.call(BODY)
            require(reply['success'] is False)
            require_equal(C.tool_rows(w),[])
        await C.dispatch(w,native)
        for args in ({'owner':'other:owner'},{'task_id':w.task},{'limit':500}):
            try:N._request(dict(BODY,arguments=args))
            except ValueError:pass
            else:raise AssertionError('foreign authority argument admitted')


async def t_personal_chat_worker_combines_mail_calendar_and_own_orders_without_writes():
    import test_native_task_entry as E
    import test_native_mail_calendar_tools as M
    import test_calendar_capability as K
    from solvio.capabilities import gmail as G,calendar as CAL
    from solvio.agent_runtime import task_revisions as TR
    from unittest.mock import patch
    async with E.world(mode='ok') as w:
        O.register(w.router,w.ledger)
        mail=M.RecordingGmail([M.mail('m1','Rechnung','Betrag 42 EUR.')])
        calendar=K._FakeCalendar()
        G.register(w.router,G.GmailCapabilities(mail))
        CAL.register(w.router,CAL.CalendarCapabilities(calendar))
        own,waiting=task(w,'Eigenes offenes Anliegen',owner='local-owner')
        w.ledger.transition(waiting.run_id,S.PLANNING)
        w.ledger.transition(waiting.run_id,S.RUNNING)
        w.ledger.transition(waiting.run_id,S.WAITING_USER)
        task(w,'Fremdes Anliegen',owner='not-the-owner')
        accepted=await E.start_private_chat_task(w,'chat-daily-overview')
        with patch.object(CAL,'now_local',lambda:K.NOW):
            run=await E.drive(w,accepted['run_id'])
        require_equal(run.state,S.SUCCEEDED,run.failure_category)
        require_equal(w.owner_overview['offen_gesamt'],1)
        require_equal(w.owner_overview['auftraege'][0]['auftrag'],own.task_id)
        require(w.daily_calendar['count']>0)
        require_equal(mail.calls,[('search','rechnung',3,''),('message','m1')])
        require_equal((calendar.creates,calendar.updates,calendar.deletes),(0,0,0))
        require_equal(w.ledger.get_run(waiting.run_id).state,S.WAITING_USER)
        require(TR.eligibility(w.ledger,run.run_id)['eligible'])


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
