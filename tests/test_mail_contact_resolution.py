"""Contact names reach the existing draft/Face-ID path without model-made addresses."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_voice_mail_actions as voice
from solvio.communication.bindings import BindingStore
from solvio.capabilities.communication import CommunicationCapabilities, register


async def _stack(contacts):
    s = await voice._stack(voice._gate('Leite die letzte Rechnung an Alex Winter weiter.'))
    directory = tempfile.mkdtemp(dir=voice.SANDBOX)
    s.contacts = BindingStore(os.path.join(directory, 'contacts.sqlite3'))
    for alias, name, addresses in contacts:
        s.contacts.confirm(alias, name, [{'channel': 'gmail', 'value': a} for a in addresses],
                           'synthetic_owner_confirmation')
    from solvio.capabilities.gmail import GmailCapabilities
    register(s.router, CommunicationCapabilities(GmailCapabilities(s.api), s.contacts))
    return s


async def t_confirmed_name_uses_the_stored_address_and_still_requires_face_id():
    s = await _stack([('alex', 'Alex Winter', ['alex.winter@example.test'])])
    try:
        result = await s.dispatcher.tool('mail_forward').run(
            {'query': 'from:elevenlabs', 'to': 'Alex Winter'})
        require_equal(result.error, 'approval_required')
        pending = await s.storage.list_pending()
        require_equal(len(pending), 1)
        require('alex.winter@example.test' in pending[0]['task'])
        require_equal(s.api.sent, [])
    finally:
        s.contacts._db.close()
        await s.storage.close()


async def t_two_contacts_with_the_same_name_do_not_choose_a_recipient():
    s = await _stack([('alex privat', 'Alex Winter', ['private@example.test']),
                      ('alex arbeit', 'Alex Winter', ['office@example.test'])])
    try:
        result = await s.dispatcher.tool('mail_forward').run(
            {'query': 'from:elevenlabs', 'to': 'Alex Winter'})
        require_equal(result.error, 'recipient_needs_confirmation')
        require_equal((s.api.drafts, s.api.sent, await s.storage.list_pending()), ({}, [], []))
    finally:
        s.contacts._db.close()
        await s.storage.close()


async def t_multiple_addresses_need_a_choice_not_the_first_address():
    s = await _stack([('alex', 'Alex Winter', ['one@example.test', 'two@example.test'])])
    try:
        result = await s.dispatcher.tool('mail_send').run(
            {'to': 'alex', 'body': 'Ich komme um acht.'})
        require_equal(result.error, 'recipient_needs_confirmation')
        require_equal((s.api.drafts, s.api.sent, await s.storage.list_pending()), ({}, [], []))
    finally:
        s.contacts._db.close()
        await s.storage.close()


async def t_mailbox_search_is_never_a_confirmed_contact():
    s = await _stack([])
    try:
        result = await s.dispatcher.tool('mail_forward').run(
            {'query': 'from:elevenlabs', 'to': 'ElevenLabs'})
        require_equal(result.error, 'recipient_needs_confirmation')
        require_equal((s.api.drafts, s.api.sent, await s.storage.list_pending()), ({}, [], []))
        require(s.contacts.get('ElevenLabs') is None)
    finally:
        s.contacts._db.close()
        await s.storage.close()


async def t_contact_confirmation_survives_the_turn_and_executes_only_after_face_id():
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.tools.registry import attach_agent_runtime
    from solvio.tools.communication_capability_tools import CommunicationCapabilityTool
    s = await _stack([])
    try:
        ledger = AgentRunLedger(os.path.join(tempfile.mkdtemp(dir=voice.SANDBOX), 'runs.sqlite3'))
        inbox = voice._Inbox()
        runtime = Orchestrator(ledger=ledger, router=s.router, control_plane=s.cp, proactive=inbox)
        tool = CommunicationCapabilityTool('communication_confirm_binding', s.router, s.gate)
        s.dispatcher.register(tool)
        attach_agent_runtime(s.dispatcher, runtime)
        result = await tool.run({'alias':'alex', 'display_name':'Alex Winter',
            'handles':[{'channel':'gmail','value':'alex@example.test'}], 'source':'owner_contact_selection'})
        require(result.error.startswith('approval_required'), str(result))
        require(s.contacts.get('alex') is None)
        await runtime.tick()
        require(s.contacts.get('alex') is None)
        s.gate.clear()
        await voice.sign(s.cp, s.co, s.device, s.H, result.data['request_id'], s.counter)
        await runtime.tick(); await runtime.tick()
        require_equal(s.contacts.get('alex')['handles'][0]['value'], 'alex@example.test')
        require_equal(len(inbox.items), 1)
        require_equal(s.api.sent, [])
    finally:
        s.contacts._db.close()
        await s.storage.close()


async def t_contact_write_with_unknown_outcome_does_not_claim_failure_or_retry():
    from solvio.agent_runtime.orchestrator import Orchestrator
    from solvio.agent_runtime.store import AgentRunLedger
    from solvio.tools.registry import attach_agent_runtime
    from solvio.tools.communication_capability_tools import CommunicationCapabilityTool
    s = await _stack([])
    original = s.contacts.confirm
    writes = []
    def committed_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        writes.append(True)
        raise OSError('synthetic post-commit read failure')
    try:
        ledger = AgentRunLedger(os.path.join(tempfile.mkdtemp(dir=voice.SANDBOX), 'runs.sqlite3'))
        inbox = voice._Inbox()
        runtime = Orchestrator(ledger=ledger, router=s.router, control_plane=s.cp, proactive=inbox)
        tool = CommunicationCapabilityTool('communication_confirm_binding', s.router, s.gate)
        s.dispatcher.register(tool)
        attach_agent_runtime(s.dispatcher, runtime)
        result = await tool.run({'alias':'alex', 'display_name':'Alex Winter',
            'handles':[{'channel':'gmail','value':'alex@example.test'}], 'source':'owner_contact_selection'})
        require(result.error.startswith('approval_required'), str(result))
        await voice.sign(s.cp, s.co, s.device, s.H, result.data['request_id'], s.counter)
        s.contacts.confirm = committed_then_failed
        await runtime.tick(); await runtime.tick()
        require_equal(len(writes), 1)
        require_equal(s.contacts.get('alex')['handles'][0]['value'], 'alex@example.test')
        require_equal(len(inbox.items), 1)
        require('nicht sicher bestätigen' in inbox.items[0]['summary'])
        require('wurde nicht gespeichert' not in inbox.items[0]['summary'])
        require_equal(s.api.sent, [])
    finally:
        s.contacts.confirm = original
        s.contacts._db.close()
        await s.storage.close()


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
