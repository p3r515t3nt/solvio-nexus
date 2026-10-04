"""Spoken corrected recipient through local HTTPS/CLI, Gmail and bound approval.

Only speech events, native selector output, synthetic device keys and Gmail's
REST surface are supplied by the fixture. No private mailbox or provider opens.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from aiohttp import TCPConnector, web
from aiohttp.test_utils import TestClient, TestServer
import mobile_attest_helper as M
import test_gpt_live_session as V
import test_live_backend as N
from test_dashboard_task_approval import _wire
from mail_send_harness import GmailApi, sign, sent_message
from solvio import voice_endpoint as E, voice_session_proof as P
from solvio.security.mobile_approval import app_attest as AA
from solvio.agent_runtime.voice_delegate import LiveBackend
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.agent_runtime.store import AgentRunLedger
from solvio.conversation import ConversationStore
from solvio.security.mobile_approval import protocol as AP
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.capabilities import gmail as G, communication as C
from solvio.communication.bindings import BindingStore
from solvio.tools.gmail_capability_tools import gmail_capability_tools
from solvio.tools.communication_capability_tools import communication_capability_tools
from solvio.specialists import subscription as U, launcher as L, providers

GOAL = 'Sende eine Mail an Robin Winderfeld: Ich komme morgen um zehn.'
CORRECTION = ('Korrektur: Winterfeld, W-I-N-T-E-R-F-E-L-D. '
              'Sende die besprochene Mail an Robin Winterfeld: Ich komme morgen um zehn.')
RIGHT = 'winterfeld@example.test'
WRONG = 'winderfeld@example.test'
BODY = 'Ich komme morgen um zehn.'
SCRIPT = r'''import json,sys,time,pathlib
p=json.loads(sys.stdin.read().partition(chr(10))[2]); u=json.loads(p[1]['content'])
rows=u.get('core_results',[]); text=u['user_text']; name='Robin Winterfeld' if 'Winterfeld' in text else 'Robin Winderfeld'
if HOLD and ((HOLD=='first' and name.endswith('Winderfeld')) or HOLD=='second' and rows):
 pathlib.Path(ENTERED).write_text('entered')
 for _ in range(1000):
  if pathlib.Path(RELEASE).exists(): break
  time.sleep(.005)
if rows:
 name=rows[0]['arguments']['alias']
 if OTHER_TO: name=OTHER_TO
 call={'name':'mail_send','arguments':{'to':name,'subject':'Morgen','body':'Ich komme morgen um zehn.'}}
elif DIRECT:
 call={'name':'mail_send','arguments':{'to':name,'subject':'Morgen','body':'Ich komme morgen um zehn.'}}
else:
 call={'name':'communication_resolve_recipient','arguments':{'alias':name}}
reply={'calls':[call],'clarification':''}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''


@asynccontextmanager
async def world(*, direct=False, hold='', ambiguous=False, other_to='', send_ambiguous=False):
    with N.world() as (directory, _, native, launches):
        entered, release = Path(directory) / 'entered', Path(directory) / 'release'
        script = SCRIPT.replace('ENTERED', repr(str(entered))).replace('RELEASE', repr(str(release)))
        script = script.replace('HOLD', repr(hold)).replace('DIRECT', repr(direct))
        script = script.replace('OTHER_TO', repr(other_to))
        def invocation(provider, *, workdir, **kwargs):
            require_equal(provider, 'codex')
            return L.Invocation(sys.executable, ('-c', script), cwd=workdir, timeout=5)
        with patch.object(U, 'text_invocation', invocation), patch.object(providers, 'claude_status',
                AsyncMock(side_effect=AssertionError('fallback forbidden'))):
            async with V.world() as w:
                folder = Path(w.ledger.path).parent
                w.approvals, w.cp, w.co, _ = await _wire(str(folder / 'mobile'))
                w.device = await M.enroll_attested(w.cp, transport_cred='local-voice-mail')
                w.router._mobile = CapabilityApprovals(w.co, owner_principal='local-owner')
                w.router._policy_mode = 'enforce'
                w.orch.control_plane = w.cp
                w.orch.conversations = w.server.conversations
                w.api = GmailApi(send_ambiguous=send_ambiguous)
                G.register(w.router, G.GmailCapabilities(w.api))
                w.contacts = BindingStore(str(folder / 'contacts.sqlite3'))
                for alias, name, address in [('wrong', 'Robin Winderfeld', WRONG), ('right', 'Robin Winterfeld', RIGHT)]:
                    w.contacts.confirm(alias, name, [{'channel':'gmail','value':address}], 'synthetic_owner_confirmation')
                if ambiguous:
                    w.contacts.confirm('second-right', 'Robin Winterfeld',
                        [{'channel':'gmail','value':'other@example.test'}], 'synthetic_owner_confirmation')
                C.register(w.router, C.CommunicationCapabilities(G.GmailCapabilities(w.api), w.contacts))
                d = w.server.dispatcher
                d.capabilities = w.router
                for tool in [*gmail_capability_tools(w.router, w.gate), *communication_capability_tools(w.router, w.gate)]:
                    tool.ledger = w.ledger
                    d.register(tool)
                d.live_backend = LiveBackend(w.ledger, transport=native.transport, quote_adapter=native.quote_adapter)
                app = web.Application(); app['control_plane'] = w.cp; E.attach(app, w.server)
                server_tls, client_tls = V.H._tls(str(folder))
                server = TestServer(app, scheme='https')
                await server.start_server(ssl=server_tls)
                client = TestClient(server, connector=TCPConnector(ssl=client_tls)); await client.start_server()
                chat, _ = w.server.conversations.create_conversation(owner_principal='local-owner',
                    kind='text', client_request_id='spoken-mail-journey')
                foreign, _ = w.server.conversations.create_conversation(owner_principal='local-owner',
                    kind='text', client_request_id='other-chat')
                w.cid, w.foreign = chat['conversation_id'], foreign['conversation_id']
                ws = await client.ws_connect(E.PATH, headers={'X-Device-Id':w.device.device_id,
                    'X-Transport-Cred':'local-voice-mail'})
                ch = await V.H.receive(ws, 'session_challenge')
                raw = P.canonical_bytes(P.build_binding(core_instance_id=w.cp.core_instance_id,
                    device_id=w.device.device_id, session_nonce=ch['session_nonce'], conversation_id=w.cid))
                await ws.send_json({'type':'session_assertion','session_nonce':ch['session_nonce'],
                    'conversation_id':w.cid,'assertion':base64.b64encode(AA.fake_assertion(w.device.aakey, P.client_data_hash(raw), 1)).decode()})
                await ws.send_json({'type':'session_start'}); await V.H.receive(ws, 'session_ready')
                w.client, w.session, w.provider = ws, w.sessions[-1], w.providers[-1]
                w.native_launches, w.entered, w.release, w.counter = launches, entered, release, [100]
                try:
                    yield w
                finally:
                    release.touch()
                    await client.close(); await server.close(); await w.approvals.close(); w.contacts._db.close()


async def say(w, text=CORRECTION, *, key='mail', offset=1000, settle=True):
    await w.provider.events.put(V.transcript(text, offset-1000, offset, 'input-'+key))
    await w.provider.events.put(V.delegate(key, offset))
    await V.until(lambda:key in w.session._delegations)
    if settle:
        await asyncio.wait_for(w.session._tool_queue.join(), 6)


async def end(w):
    await w.client.send_json({'type':'session_end'})
    await V.H.receive(w.client, 'conversation_flushed')
    await w.client.close()
    await V.until(lambda:not w.server._busy)


async def finish(w):
    entries = w.ledger.waiting_starts()
    require_equal(len(entries), 1, 'one exact send must be awaiting Face ID')
    entry = entries[0]
    require_equal(entry['conversation_ref'], w.cid, 'spoken mail lost its original chat')
    row = w.server.conversations.delivery(w.cid, entry['delivery_ref'])
    require(row and row['source_kind']=='app' and row['principal']=='local-owner')
    require_equal(row['source_ref'], 'voice:'+w.session.session_id)
    require_equal(row['message_id'], w.session._history[-1]['message_id'])
    require_equal(w.api.sent, [])
    require_equal(len(await w.approvals.list_pending()), 1)
    require_equal([method for method,path in w.api.calls if path=='/drafts'], ['POST'])
    prepared = sent_message(next(iter(w.api.drafts.values()))['raw'])
    require_equal(str(prepared['To']), RIGHT)
    require_equal(prepared.get_content().strip(), BODY)
    # A repeated opaque callback, then another callback without a new original,
    # must not manufacture another selected/drafted/approved mail.
    before = len(w.native_launches)
    await w.provider.events.put(V.delegate('mail'))
    await w.provider.events.put(V.delegate('no-new-original', 3000))
    await V.until(lambda:'no-new-original' in w.session._delegations)
    await asyncio.wait_for(w.session._tool_queue.join(), 6)
    require_equal(len(w.native_launches), before)
    require_equal(len(w.server.conversations.deliveries(w.cid)), 1)
    require_equal(len(w.ledger.waiting_starts()), 1)
    require_equal(len(w.api.drafts), 1)
    await end(w)
    # Restart before approval only observes the same pending order.
    waiting = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
        control_plane=w.cp, conversations=w.server.conversations)
    await waiting.tick()
    require_equal(waiting.ledger.waiting_starts()[0]['request_id'], entry['request_id'])
    require_equal(w.api.sent, [])
    require_equal(len(w.api.drafts), 1)
    await sign(w.cp, w.co, w.device, M, entry['request_id'], w.counter)
    orch = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
        control_plane=w.cp, conversations=w.server.conversations)
    await orch.tick(); await orch.tick()
    require_equal(len(w.api.sent), 1)
    require_equal(str(sent_message(w.api.sent[0])['To']), RIGHT)
    messages = w.server.conversations.messages(w.cid)
    require('ist verschickt' in messages[-1]['text'], messages)
    require_equal(w.server.conversations.messages(w.foreign), [])
    require_equal(json.loads(orch.ledger.get_pending_start(entry['request_id'])['mail_outcome'])['sent'], True)
    reopened_store = ConversationStore(w.server.conversations.path).open()
    try:
        reopened = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
            control_plane=w.cp, conversations=reopened_store)
        await reopened.tick()
        require_equal(reopened_store.messages(w.cid), messages)
        require_equal(len(w.api.sent), 1)
    finally:
        reopened_store.close()


async def t_spoken_mail_ends_then_bound_face_id_sends_once_and_reopens_in_same_chat():
    async with world(direct=True) as w:
        await say(w)
        await finish(w)
        require_equal(len(w.native_launches), 1)


async def t_spelled_correction_replaces_old_recipient_and_resolver_composes_without_second_callback():
    async with world(hold='first') as w:
        await say(w, GOAL, key='old', settle=False)
        await V.until(w.entered.exists)
        await w.provider.events.put(V.transcript(CORRECTION, 1500, 2500, 'input-correction'))
        await V.until(lambda:w.session._revision==2)
        w.release.touch()
        await asyncio.wait_for(w.session._tool_queue.join(), 6)
        require_equal((w.api.drafts, w.api.sent, await w.approvals.list_pending()), ({}, [], []))
        await w.provider.events.put(V.delegate('corrected', 2500))
        await V.until(lambda:'corrected' in w.session._delegations)
        await asyncio.wait_for(w.session._tool_queue.join(), 6)
        require_equal(len(w.native_launches), 3, 'successful resolver never reached compose')
        await finish(w)
        history = w.server.conversations.messages(w.cid)
        require_equal([m['text'] for m in history if m['role']=='user'], [GOAL, CORRECTION])
        from test_gpt_live_continuation import payload
        require_equal(payload(w.native_launches[-1])['user_text'], CORRECTION)
        require_equal(payload(w.native_launches[-1])['core_results'][0]['arguments'], {'alias':'Robin Winterfeld'})
        with w.ledger._open() as db:
            messages = [row[0] for row in db.execute("SELECT message_id FROM agent_cost_activities "
                "WHERE purpose='voice_delegate' ORDER BY accepted_at")]
        require_equal(messages[-2:], [history[1]['message_id'], history[1]['message_id']])


async def t_ambiguous_corrected_contact_cannot_compose_or_gain_send_authority():
    async with world(ambiguous=True) as w:
        await say(w)
        require_equal(len(w.native_launches), 1)
        require_equal((w.api.drafts, w.api.sent, await w.approvals.list_pending()), ({}, [], []))
        require(w.session._read_continuation is None)
        require(any('Empfänger ist noch nicht eindeutig' in e.get('content', '') for e in w.provider.sent))
        await end(w)


async def t_recipient_lookup_question_has_no_original_send_authority():
    async with world() as w:
        await say(w, 'Welche Mailadresse hat Robin Winterfeld?')
        require_equal(len(w.native_launches), 1)
        require_equal((w.api.drafts, w.api.sent, await w.approvals.list_pending()), ({}, [], []))
        require(w.session._read_continuation is None)
        await end(w)


async def t_resolver_followup_cannot_replace_the_confirmed_recipient():
    async with world(other_to=WRONG) as w:
        await say(w)
        require_equal(len(w.native_launches), 2)
        require_equal((w.api.drafts, w.api.sent, await w.approvals.list_pending()), ({}, [], []))
        require_equal(w.ledger.waiting_starts(), [])
        await end(w)


async def t_correction_or_voice_end_while_compose_is_running_prevents_mail_admission():
    for change in ('correction', 'end', 'revoke'):
        async with world(hold='second') as w:
            await say(w, settle=False)
            await V.until(w.entered.exists)
            if change == 'correction':
                await w.provider.events.put(V.transcript('Nein, keine Mail senden.', 1500, 2500, 'cancel-mail'))
                await V.until(lambda:w.session._revision==2)
            elif change == 'revoke':
                await w.cp.revoke_device(w.device.device_id)
            else:
                await end(w)
            w.release.touch()
            await asyncio.wait_for(w.session._tool_queue.join(), 6)
            require_equal((w.api.drafts, w.api.sent, await w.approvals.list_pending()), ({}, [], []), change)
            require_equal(w.ledger.waiting_starts(), [], change)
            require_equal(w.server.conversations.deliveries(w.cid), [], change)
            if change != 'end':
                await end(w)


async def t_correction_close_or_revocation_during_draft_await_never_asks_or_sends_or_retries():
    for change in ('correction', 'close', 'revoke'):
        async with world(direct=True) as w:
            entered, release = asyncio.Event(), asyncio.Event()
            original = w.api._request
            async def request(method, path, **kwargs):
                if (method, path) == ('POST', '/drafts'):
                    entered.set()
                    await release.wait()
                return await original(method, path, **kwargs)
            with patch.object(w.api, '_request', request):
                await say(w, settle=False)
                await asyncio.wait_for(entered.wait(), 4)
                if change == 'correction':
                    await w.provider.events.put(V.transcript('Nein, nicht senden.', 1500, 2500, 'cancel-draft'))
                    await V.until(lambda:w.session._revision==2)
                elif change == 'revoke':
                    await w.cp.revoke_device(w.device.device_id)
                else:
                    await end(w)
                release.set()
                await asyncio.wait_for(w.session._tool_queue.join(), 6)
                require_equal(w.api.sent, [], change)
                require_equal(await w.approvals.list_pending(), [], change)
                require_equal(w.ledger.waiting_starts(), [], change)
                # An already entered draft request may have completed. It is
                # never called twice and never upgraded to a send approval.
                require(len([r for r in w.api.calls if r == ('POST', '/drafts')]) <= 1, change)
                orch = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
                    control_plane=w.cp, conversations=w.server.conversations)
                await orch.tick(); await orch.tick()
                require_equal(w.api.sent, [], change)
                require_equal(w.ledger.waiting_starts(), [], change)
                if change != 'close':
                    await end(w)


async def t_other_connection_proof_cannot_bind_spoken_mail_to_this_original():
    from dataclasses import replace
    async with world(direct=True) as w:
        w.session.app_task_session = replace(w.session.app_task_session, session_id='another-connection')
        await say(w)
        require_equal((w.api.drafts, w.api.sent, await w.approvals.list_pending()), ({}, [], []))
        require_equal(w.server.conversations.deliveries(w.cid), [])
        await end(w)


async def t_chat_deletion_or_device_revocation_after_approval_cannot_send_or_recreate_chat():
    for change in ('delete', 'revoke'):
        async with world(direct=True) as w:
            await say(w)
            entry = w.ledger.waiting_starts()[0]
            await end(w)
            await sign(w.cp, w.co, w.device, M, entry['request_id'], w.counter)
            if change == 'delete':
                w.server.conversations.delete_conversation(w.cid)
            else:
                await w.cp.revoke_device(w.device.device_id)
            orch = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
                control_plane=w.cp, conversations=w.server.conversations)
            await orch.tick(); await orch.tick()
            require_equal(w.api.sent, [], change)
            require_equal(w.server.conversations.messages(w.foreign), [], change)
            if change == 'delete':
                require_equal(w.server.conversations.messages(w.cid), [])
                require(not w.server.conversations.conversation_owned(w.cid, 'local-owner'))
            else:
                require_equal(json.loads(orch.ledger.get_pending_start(entry['request_id'])['mail_outcome'])['sent'], False)


async def t_denied_spoken_send_stays_terminal_and_reports_in_its_original_chat():
    async with world(direct=True) as w:
        await say(w)
        entry = w.ledger.waiting_starts()[0]
        await end(w)
        challenge, reason = await w.cp.issue_challenge(approval_id=entry['request_id'], device_id=w.device.device_id)
        require(challenge, reason)
        _, status = await w.co.apply_mobile_decision(**M.sign_decision(w.device,
            AP.b64d(challenge['payload_b64']), decision=AP.DECISION_DENY, counter=101))
        require_equal(status, 'ok')
        orch = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
            control_plane=w.cp, conversations=w.server.conversations)
        await orch.tick(); await orch.tick(); await orch.tick()
        require_equal(w.api.sent, [])
        require('abgelehnt' in w.server.conversations.messages(w.cid)[-1]['text'])
        require_equal(len([r for r in w.api.calls if r == ('POST', '/drafts')]), 1)
        require_equal(json.loads(orch.ledger.get_pending_start(entry['request_id'])['mail_outcome'])['sent'], False)


async def t_uncertain_spoken_provider_send_is_unknown_in_same_chat_and_never_retried():
    async with world(direct=True, send_ambiguous=True) as w:
        await say(w)
        entry = w.ledger.waiting_starts()[0]
        await end(w)
        await sign(w.cp, w.co, w.device, M, entry['request_id'], w.counter)
        orch = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
            control_plane=w.cp, conversations=w.server.conversations)
        await orch.tick(); await orch.tick(); await orch.tick()
        require_equal(len([r for r in w.api.calls if r == ('POST', '/drafts/send')]), 1)
        result = json.loads(orch.ledger.get_pending_start(entry['request_id'])['mail_outcome'])
        require_equal(result['sent'], None)
        require('nicht sicher' in w.server.conversations.messages(w.cid)[-1]['text'])


async def t_spoken_draft_changed_after_face_id_is_refused_and_result_stays_in_same_chat():
    async with world(direct=True) as w:
        await say(w)
        entry = w.ledger.waiting_starts()[0]
        await end(w)
        await sign(w.cp, w.co, w.device, M, entry['request_id'], w.counter)
        draft = next(iter(w.api.drafts.values()))
        changed = sent_message(draft['raw'])
        changed.replace_header('To', WRONG)
        draft['raw'] = changed.as_bytes()
        orch = Orchestrator(ledger=AgentRunLedger(w.ledger.path), router=w.router,
            control_plane=w.cp, conversations=w.server.conversations)
        await orch.tick(); await orch.tick()
        require_equal(w.api.sent, [])
        require_equal(len([r for r in w.api.calls if r == ('POST', '/drafts/send')]), 0)
        require('nicht' in w.server.conversations.messages(w.cid)[-1]['text'])
        require(orch.ledger.get_pending_start(entry['request_id'])['mail_outcome'])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
