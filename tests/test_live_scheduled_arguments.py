"""Existing scheduled actions keep their arguments across native selection."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime.voice_delegate import validate_selection
from solvio.tools.proactive_capability_tools import ProactiveCapabilityTool
from solvio.capabilities import proactive as P
from solvio.proactive.store import ProactiveStore
import test_live_backend as B
import test_gpt_live_continuation as C


def selected(action, args):
    call={'name':'background_create','arguments':{'titel':'Kalender','wann':'taeglich 07:30',
        'aktion':action,'argumente':args}}
    tools=[ProactiveCapabilityTool('background_create',None,None).schema()]
    return validate_selection(json.dumps({'calls':[call],'clarification':''}),tools)[0][0]


def t_existing_mail_calendar_and_home_arguments_survive_selection():
    cases={'calendar_list_events':{'when':'heute','days':1},
        'calendar_search_events':{'query':'Arbeit','days':7},
        'calendar_get_event':{'title':'Besprechung','when':'morgen'},
        'calendar_find_availability':{'when':'morgen','duration_minutes':30},
        'gmail_list_recent':{'only_unread':True,'limit':5},
        'gmail_search':{'query':'Rechnung','limit':5},
        'gmail_read_message':{'message_id':'m1'},
        'ha_get_state':{'name':'Temperatur','area':'Wohnzimmer'},
        'ha_list_devices':{'area':'Wohnzimmer','domain':'sensor'},
        'ha_turn_on':{'name':'Lampe','area':'Wohnzimmer'},
        'ha_turn_off':{'name':'Lampe','area':'Wohnzimmer'},
        'ha_set_brightness':{'name':'Lampe','brightness_pct':50},
        'tagesueberblick':{},
        'mail_antwort_pruefen':{'thread_id':'thread1','message_id':'sent1'}}
    require_equal(set(cases),set(P.ALLOWED_ACTIONS+P.EVERYDAY_ACTIONS))
    for action,args in cases.items():
        require_equal(selected(action,args)['arguments']['argumente'],args)


def t_nested_authority_and_unknown_parameters_stay_closed():
    for args in [{'query':'Rechnung','approved':True}, {'unknown':'value'},
                 {'query':{'principal':'owner'}}, {'limit':'five'},
                 {'thread_id':'a','message_id':'b','source_current':True}]:
        B.raises(lambda: selected('gmail_search',args))


async def t_actual_live_callback_stores_nested_calendar_request_without_running_it():
    args={'titel':'Kalender','wann':'taeglich 07:30',
        'aktion':'calendar_list_events','argumente':{'when':'heute'}}
    text='Leg mir Kalender an, taeglich 07:30, mit calendar_list_events fuer heute.'
    script='''import json
reply=REPLY
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''.replace('REPLY',repr({'calls':[{'name':'background_create','arguments':args}],'clarification':''}))
    with tempfile.TemporaryDirectory(prefix='solvio-schedule-fixture-') as folder:
      async with C.world(script_override=script) as w:
        store=ProactiveStore(str(Path(folder)/'tasks.sqlite3'))
        P.register(w.router,P.ProactiveCapabilities(store,gate=w.gate,router=w.router))
        tool=ProactiveCapabilityTool('background_create',w.router,w.gate)
        tool.ledger=w.ledger;w.server.dispatcher.register(tool)
        await C.first(w,text)
        result=w.session._tool_results[-1]
        require(result['success'],result)
        tasks=await store.list_tasks()
        require_equal(len(tasks),1)
        require_equal(tasks[0].action['arguments'],{'when':'heute'})
        require_equal(tasks[0].created_from,text)
        require_equal(tasks[0].owner,w.gate.context().principal)
        require_equal(tasks[0].action['capability'],'calendar_list_events')
        require(w.session._read_continuation is None)
        await C.H.end(w,w.client)


async def t_mismatched_action_arguments_are_rejected_before_task_creation():
    args={'titel':'Kalender','wann':'taeglich 07:30',
        'aktion':'calendar_get_event','argumente':{'query':'Besprechung'}}
    # Both are individually known schemas, but get_event needs title, not query.
    require_equal(selected(args['aktion'],args['argumente'])['arguments']['argumente'],args['argumente'])
    with tempfile.TemporaryDirectory(prefix='solvio-schedule-fixture-') as folder:
      async with C.world() as w:
        store=ProactiveStore(str(Path(folder)/'tasks.sqlite3'))
        P.register(w.router,P.ProactiveCapabilities(store,gate=w.gate,router=w.router))
        tool=ProactiveCapabilityTool('background_create',w.router,w.gate)
        tool.ledger=w.ledger
        await C.first(w,'Leg mir Kalender an, taeglich 07:30, mit calendar_get_event.')
        result=await tool.run(args)
        require(not result.success,result)
        require_equal(result.error,'invalid_scheduled_arguments')
        require_equal(await store.list_tasks(),[])
        await C.H.end(w,w.client)


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
