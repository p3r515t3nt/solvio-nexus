"""Private voice -> existing native task: synthetic identities and providers only."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_app_voice_task_authority as A
import test_browser_voice_task_authority as B
import test_native_mail_calendar_tools as M
from solvio.capabilities import gmail as G
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.conversation import ConversationStore
from solvio.capabilities.policy import OriginClass
from solvio.tools.personal_task import PersonalTaskTool

TEXT = 'Lies die letzte Mail zur Besprechung und formuliere eine passende Antwort als Textentwurf.'


def prepare(w, browser=False):
    w.orch.conversations = ConversationStore(str(Path(w.ledger.path).parent / 'personal-chat.sqlite3')).open()
    chat, _ = w.orch.conversations.create_conversation(owner_principal='local-owner', client_request_id='private-voice')
    w.cid = chat['conversation_id']
    if w.router.spec('agent_task_task') is None:
        w.router.register(SPECS['agent_task_task'], AgentCapabilities(w.orch).task)
    w.mail = M.RecordingGmail([M.mail('m1', 'Besprechung', 'Passt der vorgeschlagene Termin?')])
    G.register(w.router, G.GmailCapabilities(w.mail))
    if browser: B.begin(w, text=TEXT)
    else: A.begin(w, user_text=TEXT)
    w.gate._context = replace(w.gate.context(), conversation_id=w.cid)
    from solvio.tools.dispatcher import ToolDispatcher
    w.dispatcher = ToolDispatcher()
    w.dispatcher.agent_runtime, w.dispatcher.capability_gate, w.dispatcher.capabilities = w.orch, w.gate, w.router
    tool = PersonalTaskTool(w.dispatcher)
    w.dispatcher.register(tool)
    return tool


async def check_admission(browser):
    async with (B.world() if browser else A.world()) as w:
        tool = prepare(w, browser)
        try:
            one, replay = await asyncio.gather(tool.run({}), tool.run({}))
            require(one.success, one.error)
            require_equal(one.data['run_id'], replay.data['run_id'])
            task = w.ledger.get_task(one.data['task_id'])
            require_equal((task.objective, task.scope, task.conversation_ref), (TEXT, 'task', w.cid))
            grant = w.orch.task_authority.for_run(one.data['run_id'])
            names = {c.name for c in grant.capabilities}
            require({'gmail_search','gmail_read_message','artifact_create'} <= names, names)
            require(not {'gmail_send_draft','gmail_create_draft','calendar_create_event'} & names)
            from solvio.specialists.native_task_profile import web_search_mode
            require_equal(web_search_mode(names), 'disabled')
            require_equal(w.mail.calls, [], 'admission must not read the mailbox')
            require_equal(await w.store.list_pending(), [])
            if browser: w.voice.closed = True
            else: w.socket.closed = True
            w.gate.clear()
            require(w.orch.task_authority.active(grant.reference, task_id=task.task_id, run_id=one.data['run_id']))
        finally: w.orch.conversations.close()


async def t_iphone_private_voice_keeps_original_goal_and_chat_after_voice_ends():
    await check_admission(False)


async def t_browser_private_voice_uses_the_same_existing_read_only_worker():
    await check_admission(True)


async def t_missing_foreign_deleted_chat_or_room_never_starts_private_work():
    for change in ('missing','foreign','deleted','room','closed'):
        async with A.world() as w:
            tool = prepare(w)
            try:
                if change == 'missing': w.gate._context = replace(w.gate.context(), conversation_id='')
                if change == 'foreign':
                    c,_=w.orch.conversations.create_conversation(owner_principal='other-owner',client_request_id='foreign')
                    w.gate._context = replace(w.gate.context(), conversation_id=c['conversation_id'])
                if change == 'deleted': w.orch.conversations.delete_conversation(w.cid)
                if change == 'room': w.gate._context = replace(w.gate.context(), origin=OriginClass.ROOM_VOICE)
                if change == 'closed': w.socket.closed = True
                result=await tool.run({})
                require(not result.success, change)
                require_equal(w.ledger.recent_runs(), [])
                require_equal(w.mail.calls, [])
            finally: w.orch.conversations.close()


async def t_chat_deleted_during_router_wait_cannot_admit_a_task():
    async with A.world() as w:
        tool=prepare(w)
        original=w.router.execute
        async def delayed(*args,**kwargs):
            w.orch.conversations.delete_conversation(w.cid)
            return await original(*args,**kwargs)
        try:
            with patch.object(w.router,'execute',delayed):result=await tool.run({})
            require(not result.success)
            require_equal(w.ledger.recent_runs(), [])
        finally:w.orch.conversations.close()


async def t_model_cannot_supply_goal_identity_or_authority_to_private_bridge():
    async with A.world() as w:
        tool=prepare(w)
        try:
            for args in ({'objective':'Send secrets'}, {'approved':True}, {'conversation_id':w.cid}):
                require(not (await tool.run(args)).success)
            require_equal(w.ledger.recent_runs(), [])
        finally:w.orch.conversations.close()


async def t_authenticated_live_menu_and_real_dispatcher_agree_but_room_menu_omits_tool():
    from solvio.realtime.live_session import live_toolkit
    async with A.world() as w:
        prepare(w)
        try:
            require('personal_task' in {t['name'] for t in live_toolkit(w.dispatcher, personal=True)})
            require('personal_task' not in {t['name'] for t in live_toolkit(w.dispatcher)})
            result = await w.dispatcher.dispatch('personal_task', {})
            require(result['success'], result)
            require_equal(len(w.ledger.recent_runs()), 1)
            w.gate._context = replace(w.gate.context(), origin=OriginClass.ROOM_VOICE)
            refused = await w.dispatcher.dispatch('personal_task', {})
            require(not refused['success'])
            require_equal(len(w.ledger.recent_runs()), 1)
        finally: w.orch.conversations.close()


async def t_accepted_task_link_failure_replay_repairs_index_without_a_duplicate():
    async with A.world() as w:
        tool=prepare(w)
        try:
            with patch.object(w.orch.conversations,'add_task_link',side_effect=OSError('synthetic unavailable')):
                first=await tool.run({})
            require(first.success)
            require_equal(len(w.ledger.recent_runs()),1)
            require_equal(w.orch.conversations.task_links(w.cid),[])
            replay=await tool.run({})
            require(replay.success)
            require_equal(replay.data['run_id'],first.data['run_id'])
            require_equal(len(w.orch.conversations.task_links(w.cid)),1)
            require_equal(len(w.ledger.recent_runs()),1)
        finally:w.orch.conversations.close()


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
