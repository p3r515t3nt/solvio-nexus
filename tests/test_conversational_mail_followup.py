"""Real chat/approval/scheduler seams, only mailbox and device are synthetic."""
import json
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_chat_mail as chat
from solvio.capabilities.proactive import ProactiveCapabilities, register
from solvio.proactive.store import ProactiveStore
from solvio.tools.mail_followup import deadline

TEXT = 'Erinnere mich morgen um 17 Uhr, wenn auf meine gesendete Angebotsmail an Ada keine Antwort kommt.'
ARGS = {'query': 'to:ada@example.test subject:Angebot', 'wann': '2099-10-02 17:00'}
MAIL = {'id': 'sent1', 'thread_id': 'thread1', 'subject': 'Angebot', 'sent': True, 'draft': False}


def setup(w, rows=None):
    store = ProactiveStore(str(Path(w.ledger.path).parent / 'followup.sqlite3'))
    register(w.router, ProactiveCapabilities(store))
    w.router._handlers['gmail_search'] = AsyncMock(return_value={'messages': [MAIL] if rows is None else rows, 'complete': True})
    w.selection = {'calls': [{'name': 'mail_followup', 'arguments': ARGS}], 'clarification': ''}
    return store


OVERVIEW_TEXT = 'Gib mir täglich um 8 Uhr einen Überblick über meine Termine, ungelesenen Mails und offenen Aufgaben.'
OVERVIEW_ARGS = {'titel': 'Mein Tagesüberblick', 'wann': 'täglich 08:00',
                 'aktion': 'tagesueberblick', 'argumente': {}}


def overview_setup(w):
    store = ProactiveStore(str(Path(w.ledger.path).parent / 'overview.sqlite3'))
    caps = ProactiveCapabilities(store)
    register(w.router, caps)
    w.selection = {'calls': [{'name': 'background_create', 'arguments': dict(OVERVIEW_ARGS)}], 'clarification': ''}
    return store, caps


async def t_typed_daily_overview_waits_for_face_id_and_confirms_exact_plan_in_original_chat():
    async with chat.world() as w:
        store, _ = overview_setup(w); cid = await w.chat()
        other = await w.chat('unrelated-overview')
        row = await chat.send(w, cid, OVERVIEW_TEXT)
        require_equal(row['status'], 'completed', row['error_code'])
        entry = chat.pending(w)
        require_equal(entry['arguments'], OVERVIEW_ARGS)
        require_equal((entry['conversation_ref'], entry['delivery_ref']), (cid, row['delivery_id']))
        require_equal((await store.list_tasks(), w.api.sent), ([], []))
        require('Noch ist er nicht eingerichtet' in w.chat_store.messages(cid)[-1]['text'])
        await chat.sign(w.cp, w.co, w.ctx, chat.H, entry['request_id'], w.counter)
        await w.processor.shutdown()
        await chat.restarted(w).tick(); await chat.restarted(w).tick()
        tasks = await store.list_tasks()
        require_equal(len(tasks), 1)
        require_equal((tasks[0].owner, tasks[0].action['kind'], tasks[0].schedule['uhrzeit']),
                      ('local-owner', 'tagesueberblick', '08:00'))
        require('Tagesüberblick ist eingerichtet' in w.chat_store.messages(cid)[-1]['text'])
        messages = w.chat_store.messages(cid)
        await chat.restarted(w).tick()
        require_equal(w.chat_store.messages(cid), messages)
        require_equal(w.chat_store.messages(other), [])
        require_equal((w.select_count, len(await store.list_tasks()), w.api.sent), (1, 1, []))


async def t_daily_overview_missing_time_asks_back_and_invalid_schedule_opens_no_approval():
    for selector_question in (True, False):
        async with chat.world() as w:
            store, _ = overview_setup(w); cid = await w.chat()
            if selector_question:
                w.selection = {'calls': [], 'clarification': 'Um wie viel Uhr möchtest du den Tagesüberblick?'}
            else:
                w.selection['calls'][0]['arguments'] = dict(OVERVIEW_ARGS, wann='täglich')
            row = await chat.send(w, cid, 'Gib mir täglich einen Überblick über meine Termine und Mails.')
            require_equal(row['status'], 'completed', row['error_code'])
            require_equal((w.ledger.waiting_starts(), await store.list_tasks(), w.api.sent), ([], [], []))
            require('eingerichtet' not in w.chat_store.messages(cid)[-1]['text'])


async def t_typed_schedule_selector_cannot_request_other_actions_or_authority():
    for altered in (dict(OVERVIEW_ARGS, aktion='ha_turn_off'),
                    dict(OVERVIEW_ARGS, aktion='recherche'),
                    dict(OVERVIEW_ARGS, argumente={'approved': True}),
                    dict(OVERVIEW_ARGS, principal='another-owner')):
        async with chat.world() as w:
            store, _ = overview_setup(w)
            w.selection['calls'][0]['arguments'] = altered
            row = await chat.send(w, await w.chat(), OVERVIEW_TEXT)
            require_equal(row['status'], 'blocked')
            require_equal((w.ledger.waiting_starts(), await store.list_tasks(), w.api.sent), ([], [], []))


async def t_daily_overview_denial_and_deleted_original_chat_prevent_creation():
    from solvio.security.mobile_approval import protocol as P
    for removed in (False, True):
        async with chat.world() as w:
            store, _ = overview_setup(w); cid = await w.chat()
            await chat.send(w, cid, OVERVIEW_TEXT); entry = chat.pending(w)
            if removed:
                await chat.sign(w.cp, w.co, w.ctx, chat.H, entry['request_id'], w.counter)
                w.chat_store.delete_conversation(cid)
            else:
                wire, reason = await w.cp.issue_challenge(approval_id=entry['request_id'], device_id=w.ctx.device_id)
                require(wire is not None, reason)
                _, status = await w.co.apply_mobile_decision(**chat.H.sign_decision(w.ctx,
                    P.b64d(wire['payload_b64']), decision=P.DECISION_DENY, counter=101))
                require_equal(status, 'ok')
            await chat.restarted(w).tick(); await chat.restarted(w).tick()
            require_equal((await store.list_tasks(), w.ledger.waiting_starts(), w.api.sent), ([], [], []))
            if not removed:
                require('Tagesüberblick abgelehnt' in w.chat_store.messages(cid)[-1]['text'])
                again = await chat.send(w, cid, OVERVIEW_TEXT, counter=2)
                require_equal(w.select_count, 1)
                require_equal(again['status'], 'completed')


async def t_daily_overview_journal_cannot_adopt_different_arguments_from_original_delivery():
    from solvio.conversation.mail import source_matches
    async with chat.world() as w:
        store, _ = overview_setup(w); cid = await w.chat()
        await chat.send(w, cid, OVERVIEW_TEXT); entry = chat.pending(w)
        require(await source_matches(w.chat_store, entry, w.cp))
        changed = dict(entry, arguments=dict(OVERVIEW_ARGS, wann='täglich 09:00'))
        require_equal(await source_matches(w.chat_store, changed, w.cp), False)
        require_equal(await store.list_tasks(), [])


async def t_daily_overview_lost_creation_result_is_unknown_and_never_recreated():
    async with chat.world() as w:
        store, caps = overview_setup(w); cid = await w.chat()
        await chat.send(w, cid, OVERVIEW_TEXT); entry = chat.pending(w)
        await chat.sign(w.cp, w.co, w.ctx, chat.H, entry['request_id'], w.counter)
        async def lose_result(arguments):
            await caps.create(arguments)
            raise RuntimeError('synthetic result lost after creation')
        w.router._handlers['background_create'] = lose_result
        await chat.restarted(w).tick(); await chat.restarted(w).tick()
        require_equal(len(await store.list_tasks()), 1)
        require('nicht bestätigt' in w.chat_store.messages(cid)[-1]['text'])
        require('ist eingerichtet' not in w.chat_store.messages(cid)[-1]['text'])
        await chat.restarted(w).tick()
        require_equal((len(await store.list_tasks()), w.select_count, w.api.sent), (1, 1, []))


def t_deadline_is_one_occurrence_with_explicit_time_and_no_relative_approval_drift():
    now = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
    for text, expected in [('Freitag um 17 Uhr', '2026-10-02 17:00'),
                           ('morgen 09:30', '2026-09-30 09:30'),
                           ('in zwei Stunden', '2026-09-29 12:00')]:
        require_equal(deadline(text, now=now), expected)
    for text in ['Freitag', 'morgen', 'täglich 17:00', 'heute 08:00', 'morgen 25:00']:
        require_equal(deadline(text, now=now), None)
    spring = datetime(2026, 3, 28, 12, tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
    require_equal(deadline('morgen 02:30', now=spring), None)


async def t_typed_reminder_approves_once_survives_chat_shutdown_and_reports_in_same_chat():
    async with chat.world() as w:
        store = setup(w); cid = await w.chat()
        row = await chat.send(w, cid, TEXT)
        require_equal(row['status'], 'completed', row['error_code'])
        entry = chat.pending(w)
        require_equal(entry['capability'], 'background_create')
        require_equal(entry['arguments']['argumente'], {'thread_id': 'thread1', 'message_id': 'sent1'})
        require_equal(entry['conversation_ref'], cid)
        require_equal(await store.list_tasks(), [])
        require_equal((w.api.drafts, w.api.sent), ({}, []))
        require('Noch ist sie nicht eingerichtet' in w.chat_store.messages(cid)[-1]['text'])
        await chat.sign(w.cp, w.co, w.ctx, chat.H, entry['request_id'], w.counter)
        events=[]; w.router._recorder=events.append
        await w.processor.shutdown()
        await chat.restarted(w).tick(); await chat.restarted(w).tick()
        tasks = await store.list_tasks(); require_equal(len(tasks), 1, str([(e.phase,e.outcome,e.detail) for e in events]))
        require_equal(tasks[0].owner, 'local-owner')
        require_equal(tasks[0].action['kind'], 'mail_antwort_pruefen')
        require('Erinnerung ist eingerichtet' in w.chat_store.messages(cid)[-1]['text'])
        require_equal(w.api.sent, [])


async def t_ambiguous_or_unsent_mail_never_opens_approval_or_chooses_latest():
    for rows in [[], [MAIL, dict(MAIL, id='sent2')], [dict(MAIL, sent=False)], [dict(MAIL, draft=True)]]:
        async with chat.world() as w:
            store = setup(w, rows); cid = await w.chat()
            row = await chat.send(w, cid, TEXT)
            require_equal(row['status'], 'completed')
            require_equal((w.ledger.waiting_starts(), await store.list_tasks(), w.api.sent), ([], [], []))
            require('Face ID' not in w.chat_store.messages(cid)[-1]['text'])


async def t_missing_time_asks_in_chat_without_reading_mail_or_requesting_authority():
    async with chat.world() as w:
        store = setup(w); cid = await w.chat()
        w.selection['calls'][0]['arguments'] = dict(ARGS, wann='Freitag')
        await chat.send(w, cid, TEXT)
        require_equal(w.router._handlers['gmail_search'].await_count, 0)
        require_equal((w.ledger.waiting_starts(), await store.list_tasks()), ([], []))
        require('Uhrzeit' in w.chat_store.messages(cid)[-1]['text'])


async def t_denial_and_removed_chat_do_not_create_reminders_on_restart():
    from solvio.security.mobile_approval import protocol as P
    for removed in (False, True):
        async with chat.world() as w:
            store = setup(w); cid = await w.chat()
            await chat.send(w, cid, TEXT); entry = chat.pending(w)
            if removed:
                await chat.sign(w.cp, w.co, w.ctx, chat.H, entry['request_id'], w.counter)
                w.chat_store.delete_conversation(cid)
            else:
                wire, reason = await w.cp.issue_challenge(approval_id=entry['request_id'], device_id=w.ctx.device_id)
                require(wire is not None, reason)
                _, status = await w.co.apply_mobile_decision(**chat.H.sign_decision(w.ctx,
                    P.b64d(wire['payload_b64']), decision=P.DECISION_DENY, counter=101))
                require_equal(status, 'ok')
            await chat.restarted(w).tick(); await chat.restarted(w).tick()
            require_equal((await store.list_tasks(), w.ledger.waiting_starts(), w.api.sent), ([], [], []))
            if not removed:
                require('Erinnerung abgelehnt' in w.chat_store.messages(cid)[-1]['text'])


async def t_chat_selector_receives_only_same_chat_prior_context():
    async with chat.world() as w:
        setup(w); cid = await w.chat(); other = await w.chat('other-followup-chat')
        w.chat_store.add_message(cid, 'user', 'Gemeint ist meine Angebotsmail an Ada.')
        w.chat_store.add_message(other, 'user', 'OTHER_CHAT_PRIVATE_SENTINEL')
        captured=[]; original=w.cli.override
        def capture(kind, record):
            if chat.mail.MARKER in record['prompt']:captured.append(record['prompt'])
            return original(kind, record)
        w.cli.override=capture
        await chat.send(w, cid, 'Erinnere mich morgen um 17 Uhr, wenn auf diese Mail keine Antwort kommt.')
        require_equal(len(captured), 1)
        require('Angebotsmail an Ada' in captured[0], captured[0])
        require('OTHER_CHAT_PRIVATE_SENTINEL' not in captured[0])


async def t_voice_tool_uses_existing_scheduler_and_real_face_id():
    import tempfile
    import test_voice_mail_actions as voice
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.tools.registry import attach_agent_runtime
    from solvio.realtime.live_session import live_toolkit
    s = await voice._stack(voice._gate(TEXT))
    try:
        with tempfile.TemporaryDirectory() as folder:
            store=ProactiveStore(str(Path(folder)/'tasks.sqlite3'));register(s.router,ProactiveCapabilities(store))
            s.router._handlers['gmail_search']=AsyncMock(return_value={'messages':[MAIL], 'complete': True})
            ledger=AgentRunLedger(str(Path(folder)/'runs.sqlite3'))
            orch=Orchestrator(ledger=ledger,router=s.router,control_plane=s.cp,proactive=voice._Inbox())
            attach_agent_runtime(s.dispatcher,orch)
            require('mail_followup' in {t['name'] for t in live_toolkit(s.dispatcher)})
            result=await s.dispatcher.tool('mail_followup').run(ARGS)
            require_equal(result.error,'approval_required')
            require_equal(await store.list_tasks(),[])
            # A model retry cannot create another approval even with different arguments.
            retry=await s.dispatcher.tool('mail_followup').run(dict(ARGS,wann='2099-11-02 17:00'))
            require_equal(retry.error,'mail_turn_already_used')
            await chat.sign(s.cp,s.co,s.device,s.H,result.data['request_id'],s.counter)
            events=[]; s.router._recorder=events.append
            await orch.tick(); await orch.tick()
            require_equal(len(await store.list_tasks()),1, str([(e.phase,e.outcome,e.detail) for e in events]))
            require_equal(s.api.sent,[])
    finally:await s.storage.close()


async def t_real_gmail_search_requires_complete_page_and_all_details_before_approval():
    from solvio.capabilities.gmail import GmailCapabilities
    from solvio.integrations.gmail import Gmail
    for page,missing,expected in [(True,True,0),(False,True,0),(True,False,0),(False,False,1)]:
        async with chat.world() as w:
            store=setup(w); cid=await w.chat()
            provider=Gmail(client_id='synthetic')
            async def request(method,path,**kwargs):
                require_equal(method,'GET')
                if path=='/messages':
                    listing={'messages':[{'id':'sent1'}] + ([{'id':'gone'}] if missing else [])}
                    if page:listing['nextPageToken']='more-matches'
                    return listing
                if path=='/messages/gone':return None
                return {'id':'sent1','threadId':'thread1','labelIds':['SENT'],'internalDate':'1000',
                        'payload':{'headers':[{'name':'Subject','value':'Angebot'}]}}
            provider._request=request
            w.router._handlers['gmail_search']=GmailCapabilities(provider).search
            await chat.send(w,cid,TEXT)
            require_equal(len(w.ledger.waiting_starts()),expected)
            require_equal(await store.list_tasks(),[])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
