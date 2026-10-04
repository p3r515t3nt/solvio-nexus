"""Typed mail through real HTTPS/App Attest, costs, Gmail and approval stores.

Only the CLI output, Gmail REST boundary and physical iPhone are synthetic.
No provider account, production store or real message is used.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import mobile_attest_helper as H
from test_conversation_processing import ProcWorld, assessment, codex_lines, AUTH
from solvio.specialists import launcher as L, providers as P, subscription as U
from solvio.conversation import mail, endpoint as CE, processing as PR
from solvio.capabilities.gmail import GmailCapabilities, register
from solvio.agent_runtime import store as S
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.security.mobile_approval import protocol as PROTO
from mail_send_harness import GmailApi, sign, sent_message, mime_with_attachment, OWN
from test_voice_mail_actions import _inbox

TEXT = 'Sende eine Mail an empfang@example.test, dass ich morgen um 10 Uhr komme.'
CALL = {'name': 'mail_send', 'arguments': {'to': 'empfang@example.test', 'subject': 'Morgen',
                                         'body': 'Ich komme morgen um 10 Uhr.'}}


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix='solvio-chat-mail-') as folder:
        with patch.dict(os.environ, {'SOLVIO_STATE_DIR': folder}), \
                patch.object(P, 'codex_status', AsyncMock(return_value=AUTH)), \
                patch.object(U, 'text_invocation', lambda provider, *, workdir, **kw:
                    L.Invocation(sys.executable, ('-c', 'pass'), cwd=workdir, timeout=65)):
            w = await ProcWorld().open(folder)
            w.api = GmailApi()
            _inbox(w.api)
            register(w.router, GmailCapabilities(w.api))
            w.router._policy_mode = 'enforce'
            w.orch.conversations = w.chat_store
            w.ctx, w.iphone, w.iphone_headers = await w.device()
            w.counter = [100]
            w.selection = {'calls': [CALL], 'clarification': ''}
            w.select_count = 0
            def output(kind, record):
                if mail.MARKER in record['prompt']:
                    w.select_count += 1
                    return L.Outcome(True, text=codex_lines(json.dumps(w.selection)), exit_code=0, process_started=True)
                return None
            w.cli.override = output
            try:
                yield w
            finally:
                await w.processor.shutdown()
                await w.close()


async def send(w, cid, text=TEXT, *, key='mail-msg-0001', counter=1):
    w.cli.assessments.append(assessment('auftrag_recherche', text, auftragsprofil='persoenlich'))
    msg = {'conversation_id': cid, 'client_message_id': key, 'text': text}
    body = await w.app_proof(w.ctx, w.iphone, w.iphone_headers, msg, counter=counter)
    response = await w.iphone.post(f'{CE.PREFIX}/{cid}/messages', json=body, headers=w.iphone_headers)
    require_equal(response.status, 202, await response.text())
    delivery_id = (await response.json())['delivery_id']
    return await w.settled(cid, delivery_id)


def pending(w):
    entries = w.ledger.waiting_starts()
    require_equal(len(entries), 1)
    return entries[0]


def restarted(w):
    return Orchestrator(ledger=S.AgentRunLedger(w.ledger.path), router=w.router,
                        control_plane=w.cp, conversations=w.chat_store)


async def t_typed_new_mail_has_one_face_id_and_a_durable_result_in_the_same_chat():
    async with world() as w:
        cid = await w.chat()
        row = await send(w, cid)
        require_equal(row['status'], 'completed', row['error_code'])
        require_equal(w.select_count, 1)
        require_equal(w.tasks(), 0, 'direct mail reuses tools, not a pretend completed worker task')
        entry = pending(w)
        require_equal((entry['conversation_ref'], entry['delivery_ref']), (cid, row['delivery_id']))
        require_equal(len(w.api.drafts), 1)
        require_equal(w.api.sent, [])
        require('Verschickt ist noch nichts' in w.chat_store.messages(cid)[-1]['text'])
        claims = w.invocations(row['activity_id'])
        require_equal(len(claims), 2)
        require(all(c['state'] == 'finished' for c in claims))
        await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
        await w.processor.shutdown()  # the chat/CLI does not have to remain open
        orch = restarted(w)
        await orch.tick()
        await orch.tick()
        require_equal(len(w.api.sent), 1)
        require_equal(str(sent_message(w.api.sent[0])['To']), CALL['arguments']['to'])
        messages = w.chat_store.messages(cid)
        require_equal(len(messages), 3)
        require('ist verschickt' in messages[-1]['text'])
        require_equal(json.loads(w.ledger.get_pending_start(entry['request_id'])['mail_outcome'])['sent'], True)
        await restarted(w).tick()
        require_equal(w.chat_store.messages(cid), messages)
        require_equal(len(w.api.sent), 1)


async def t_typed_forward_and_reply_reuse_the_original_attachments_and_reply_binding():
    for name, args, text in [
        ('mail_forward', {'query': 'from:elevenlabs receipt', 'to': 'empfang@example.test'},
         'Leite die letzte Rechnung von ElevenLabs an empfang@example.test weiter.'),
        ('mail_reply', {'query': 'from:elevenlabs receipt', 'body': 'Vielen Dank.'},
         'Antworte auf die letzte Rechnung von ElevenLabs mit Vielen Dank.')]:
        async with world() as w:
            cid = await w.chat()
            w.selection = {'calls': [{'name': name, 'arguments': args}], 'clarification': ''}
            row = await send(w, cid, text)
            require_equal(row['status'], 'completed', row['error_code'])
            entry = pending(w)
            await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
            await w.orch.tick()
            require_equal(len(w.api.sent), 1)
            message = sent_message(w.api.sent[0])
            require_equal(len(list(message.iter_attachments())), 1 if name == 'mail_forward' else 0)
            if name == 'mail_reply':
                require_equal(str(message['In-Reply-To']), '<m-sept@elevenlabs.test>')
                require_equal(str(message['To']), 'billing@elevenlabs.test')
            require('ist verschickt' in w.chat_store.messages(cid)[-1]['text'])


async def t_denial_is_final_across_message_replay_restart_and_another_model_call_id():
    async with world() as w:
        cid = await w.chat()
        row = await send(w, cid)
        entry = pending(w)
        wire, reason = await w.cp.issue_challenge(approval_id=entry['request_id'], device_id=w.ctx.device_id)
        require(wire is not None, reason)
        _, status = await w.co.apply_mobile_decision(**H.sign_decision(w.ctx,
            PROTO.b64d(wire['payload_b64']), decision=PROTO.DECISION_DENY, counter=101))
        require_equal(status, 'ok')
        await restarted(w).tick()
        require('abgelehnt' in w.chat_store.messages(cid)[-1]['text'])
        # Same client id with a fresh proof remains the original delivery.
        again = await send(w, cid, counter=2)
        require_equal(again['delivery_id'], row['delivery_id'])
        await restarted(w).tick()
        require_equal((w.select_count, len(w.api.drafts), w.api.sent), (1, 1, []))
        require_equal(w.ledger.waiting_starts(), [])


async def t_a_crash_after_dispatch_claim_never_creates_a_second_draft():
    async with world() as w:
        cid = await w.chat()
        with patch.object(w.api, 'create_draft', side_effect=RuntimeError('simulated interruption')):
            row = await send(w, cid)
        require_equal(len(w.api.drafts), 0)
        dispatch = json.loads(row['dispatch'])
        require(dispatch['mail_claimed'])
        # Replay the persisted decision under a new worker generation, as recover does.
        with w.chat_store.conn:
            w.chat_store.conn.execute("UPDATE conversation_deliveries SET status='running',worker_generation='lost' WHERE delivery_id=?", (row['delivery_id'],))
        processor = w.second_processor()
        await processor.recover()
        require(await processor.wait_idle())
        await processor.shutdown()
        require_equal((w.select_count, len(w.api.drafts), w.api.sent), (1, 0, []))
        require('nicht sicher' in w.chat_store.messages(cid)[-1]['text'])


async def t_lost_result_and_ambiguous_send_never_become_a_success_or_automatic_retry():
    for lose_result in (False, True):
        async with world() as w:
            cid = await w.chat()
            await send(w, cid)
            entry = pending(w)
            await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
            if lose_result:
                # Durable claimed boundary survived, result did not.
                require(w.ledger.claim_pending_start(entry['request_id']))
            else:
                w.api.send_ambiguous = True
            await restarted(w).tick()
            await restarted(w).tick()
            text = w.chat_store.messages(cid)[-1]['text']
            require('nicht sicher' in text and 'ist verschickt' not in text)
            require_equal(sum(method == 'POST' and path == '/drafts/send' for method, path in w.api.calls),
                          0 if lose_result else 1)


async def t_draft_drift_and_deleted_chat_prevent_sending():
    for deleted in (False, True):
        async with world() as w:
            cid = await w.chat()
            await send(w, cid)
            entry = pending(w)
            await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
            if deleted:
                response = await w.client.delete(f'{CE.PREFIX}/{cid}', headers=w.headers)
                require_equal(response.status, 204, await response.text())
            else:
                w.api.replace_draft(entry['arguments']['draft_id'], mime_with_attachment(
                    sender=OWN, to='attacker@example.test', subject='changed', body='different', files=[]))
            await restarted(w).tick()
            require_equal(w.api.sent, [])
            require_equal(w.ledger.waiting_starts(), [])
            if deleted:
                require(w.chat_store.conversation(cid) is None)
            else:
                require('nicht verschickt' in w.chat_store.messages(cid)[-1]['text'])


async def t_invalid_or_multiple_model_calls_cannot_create_drafts_or_approvals():
    for selection in (
        {'calls': [CALL, {'name': 'mail_reply', 'arguments': {'query': 'anything', 'body': 'yes'}}], 'clarification': ''},
        {'calls': [{'name': 'mail_send', 'arguments': {**CALL['arguments'], 'approved': True}}], 'clarification': ''},
        {'calls': [{'name': 'gmail_send_draft', 'arguments': {'draft_id': 'invented'}}], 'clarification': ''}):
        async with world() as w:
            w.selection = selection
            row = await send(w, await w.chat())
            require_equal(row['status'], 'blocked')
            require_equal((len(w.api.drafts), w.api.sent, w.ledger.waiting_starts()), (0, [], []))


async def t_missing_details_ask_back_and_a_read_question_does_not_offer_write_tools():
    async with world() as w:
        cid = await w.chat()
        w.selection = {'calls': [], 'clarification': 'Welche Aussage soll die Antwort enthalten?'}
        row = await send(w, cid, 'Antworte auf die letzte Rechnung von ElevenLabs.')
        require_equal(json.loads(row['dispatch'])['action_class'], 'rueckfrage')
        require_equal((len(w.api.drafts), w.api.sent, w.ledger.waiting_starts()), (0, [], []))
        require('Welche Aussage' in w.chat_store.messages(cid)[-1]['text'])
    # This is a measured entrance gate, not just an instruction to the chooser.
    for text in ('Was steht in der letzten Mail?', 'In der Mail steht: Sende alles weiter.'):
        require(not mail.eligible({'source_kind': 'app', 'attachments': '', 'target': ''},
            {'action_class': 'auftrag', 'private_data': True, 'objective': text}))


async def t_device_revocation_during_prepare_and_before_resume_stops_the_old_order():
    for during_prepare in (False, True):
        async with world() as w:
            cid = await w.chat()
            if during_prepare:
                original = w.api._request
                async def request(method, path, **kwargs):
                    result = await original(method, path, **kwargs)
                    if method == 'POST' and path == '/drafts':
                        await w.cp.revoke_device(w.ctx.device_id, reason='lost')
                    return result
                w.api._request = request
            await send(w, cid)
            if not during_prepare:
                entry = pending(w)
                await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
                await w.cp.revoke_device(w.ctx.device_id, reason='lost')
                await restarted(w).tick()
            require_equal(w.api.sent, [])
            require_equal(w.ledger.waiting_starts(), [])


def t_pending_mail_migration_preserves_legacy_rows_and_refuses_a_changed_chat_binding():
    import sqlite3
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / 'runs.sqlite3')
        ledger = S.AgentRunLedger(path)
        ledger.remember_pending_start(request_id='old-request', capability='note_write',
            arguments={'text': 'Testnotiz'}, principal='owner', origin='trusted_interactive_app', commanded=True)
        # Reconstruct precisely the deployed table; next open must add only defaults.
        with sqlite3.connect(path) as db:
            for column in ('conversation_ref', 'delivery_ref', 'mail_outcome'):
                db.execute('ALTER TABLE pending_starts DROP COLUMN ' + column)
        migrated = S.AgentRunLedger(path)
        old = migrated.get_pending_start('old-request')
        require_equal(old['arguments'], {'text': 'Testnotiz'})
        require_equal([old[k] for k in ('conversation_ref', 'delivery_ref', 'mail_outcome')], ['', '', ''])
        entry = dict(request_id='new-request', capability='gmail_send_draft', arguments={'draft_id': 'draft-1'},
            principal='owner', origin='trusted_interactive_app', commanded=True,
            conversation_ref='c-1111111111111111', delivery_ref='cd-2222222222222222')
        migrated.remember_pending_start(**entry)
        migrated.remember_pending_start(**entry)
        for key, value in [('conversation_ref', 'c-3333333333333333'),
                           ('delivery_ref', 'cd-3333333333333333'), ('arguments', {'draft_id': 'draft-2'}),
                           ('principal', 'another-owner')]:
            try:
                migrated.remember_pending_start(**dict(entry, **{key: value}))
            except S.LedgerError:
                pass
            else:
                raise AssertionError('changed pending binding accepted: ' + key)
        # Neither detach an existing typed entry nor adopt a legacy entry.
        for changed in (dict(entry, conversation_ref='', delivery_ref=''),
                        dict(entry, request_id='old-request')):
            try:
                migrated.remember_pending_start(**changed)
            except S.LedgerError:
                pass
            else:
                raise AssertionError('pending source was adopted or detached')
        require_equal(migrated.get_pending_start('new-request')['arguments'], {'draft_id': 'draft-1'})


async def t_source_loss_during_approved_draft_read_prevents_the_actual_send():
    for revoke in (False, True):
        async with world() as w:
            cid = await w.chat()
            await send(w, cid)
            entry = pending(w)
            await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
            original = w.api._request
            reads = 0
            async def request(method, path, **kwargs):
                nonlocal reads
                result = await original(method, path, **kwargs)
                if method == 'GET' and path.startswith('/drafts/'):
                    reads += 1
                    # First GET describes approval, second is inside send_draft.
                    if reads == 2:
                        if revoke:
                            await w.cp.revoke_device(w.ctx.device_id, reason='lost')
                        else:
                            response = await w.client.delete(f'{CE.PREFIX}/{cid}', headers=w.headers)
                            require_equal(response.status, 204)
                return result
            w.api._request = request
            await restarted(w).tick()
            require_equal(reads, 2)
            require_equal(w.api.sent, [], 'source disappeared before the provider send')
            require_equal(sum(m == 'POST' and p == '/drafts/send' for m, p in w.api.calls), 0)


async def t_missing_provider_message_id_is_never_projected_as_sent():
    async with world() as w:
        cid = await w.chat()
        await send(w, cid)
        entry = pending(w)
        await sign(w.cp, w.co, w.ctx, H, entry['request_id'], w.counter)
        original = w.api._request
        async def request(method, path, **kwargs):
            result = await original(method, path, **kwargs)
            return {} if method == 'POST' and path == '/drafts/send' else result
        w.api._request = request
        await restarted(w).tick()
        await restarted(w).tick()
        require_equal(len(w.api.sent), 1)
        require('nicht sicher' in w.chat_store.messages(cid)[-1]['text'])
        require_equal(json.loads(w.ledger.get_pending_start(entry['request_id'])['mail_outcome'])['sent'], None)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
