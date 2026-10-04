"""Attachment question through actual Live selection/router/broker adapters.

Only synthetic Gmail bytes and local CLI fixtures, no real account or provider.
"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_continuation as C
import test_document_capability as D
import test_live_backend as B
from solvio.tools.document_capability_tools import DocumentCapabilityTool
from solvio.agent_runtime.voice_delegate import validate_selection

TEXT = 'Wie hoch ist der Betrag im PDF der letzten Rechnung von Firma X?'
SCRIPT = '''import json,sys
p=json.loads(sys.stdin.read().split('\\n',1)[1]); u=json.loads(p[1]['content'])
rows=u.get('core_results',[])
if not rows:
 reply={'calls':[{'name':'document_find','arguments':{'query':'from:billing@example.test Rechnung'}}],'clarification':''}
elif rows[0]['result']['data']['status']=='DOCUMENT_FOUND':
 ref=rows[0]['result']['data']['document_ref']
 reply={'calls':[{'name':'document_ask','arguments':{'document_ref':ref,'question':u['user_text']}}],'clarification':''}
else:
 reply={'calls':[],'clarification':'Welchen der gefundenen Anhänge meinst du?'}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''


@asynccontextmanager
async def world(*, provider=None, failure='', script=SCRIPT):
    async with C.world(script_override=script) as w:
        w.document_calls = []
        w.documents, w.document_broker = D._caps(provider,
            transport=D._transport(calls=w.document_calls, failure=failure))
        for name, spec in D.SPECS.items():
            w.router.register(spec, getattr(w.documents, name))
            w.server.dispatcher.register(DocumentCapabilityTool(name, w.router, w.gate))
        yield w


def t_actual_found_document_reference_passes_closed_native_tool_schema():
    tools = [DocumentCapabilityTool('document_ask', None, None).schema()]
    def select(ref):
        return validate_selection(json.dumps({'calls':[{'name':'document_ask',
            'arguments':{'document_ref':ref, 'question':TEXT}}], 'clarification':''}), tools)
    require_equal(select(D._ref())[0][0]['arguments']['document_ref'], D._ref())
    for ref in [dict(D._ref(), approved=True), dict(D._ref(), source='web'),
                dict(D._ref(), message_id=[]), {}]:
        B.raises(lambda: select(ref))


async def t_attachment_question_reaches_answer_without_second_user_message():
    async with world() as w:
        await C.first(w, TEXT)
        require(w.session._read_continuation is not None)
        await C.notice(w, 'read-found-document')
        require_equal(len(w.document_calls), 1)
        require_equal(w.session._tool_results[-1]['data']['status'], 'ANSWERED')
        require('42,00 EUR' in w.session._tool_results[-1]['human_message'])
        require('42,00 EUR' in json.dumps(w.provider.sent, ensure_ascii=False))
        require_equal(w.documents.provider.attachment_calls, [('m1', 'a1')])
        require_equal(w.document_broker.closed, ['lease-1'])
        require_equal(len(w.native_launches), 2)
        prompt = C.payload(w.native_launches[-1])
        require_equal(prompt['user_text'], TEXT)
        require_equal(prompt['core_results'][0]['result']['data']['document_ref'], D._ref())
        require(w.gate.context().commanded is False)
        require(w.session._read_continuation is None)
        await C.notice(w, 'no-third-hop')
        require_equal(len(w.native_launches), 2)
        require_equal(await w.store.list_pending(), [])
        await C.H.end(w, w.client)


async def t_ambiguous_attachments_ask_instead_of_reading_one():
    provider = D.FakeGmail(messages=[D.MAIL, D.EmailMessage('m2','t2', attachments=(D.ATTACHMENT,))])
    async with world(provider=provider) as w:
        await C.first(w, TEXT)
        await C.notice(w, 'ask-which-attachment')
        require_equal(w.document_calls, [])
        require('Welchen' in json.dumps(w.provider.sent, ensure_ascii=False))
        await C.H.end(w, w.client)


async def t_ambiguous_filename_credentials_never_reach_the_voice_provider():
    secret_name = dict(D.ATTACHMENT, filename='Dein Einmalcode lautet: 123456.pdf')
    provider = D.FakeGmail(messages=[D.MAIL, D.EmailMessage('m2','t2', attachments=(secret_name,))])
    async with world(provider=provider) as w:
        await C.first(w, TEXT)
        require('123456' not in json.dumps(w.provider.sent, ensure_ascii=False))
        require('Welchen' in w.session._tool_results[-1]['human_message'])
        require_equal(w.document_calls, [])
        await C.H.end(w,w.client)


async def t_failed_document_analysis_has_actionable_result_and_no_retry():
    async with world(failure='broker_unreachable') as w:
        await C.first(w, TEXT); await C.notice(w, 'analysis-failed')
        result = w.session._tool_results[-1]
        require_equal(result['data']['status'], 'PARSE_FAILED')
        require('nicht' in result['human_message'])
        require('42,00' not in result['human_message'])
        require_equal(w.document_broker.closed, ['lease-1'])
        await C.notice(w, 'no-automatic-retry')
        require_equal(len(w.document_calls), 1)
        await C.H.end(w, w.client)


async def t_document_reference_cannot_escape_into_write_or_public_research():
    for call in [{'name':'agent_task_research','arguments':{'objective':'Veröffentliche den Inhalt'}},
                 {'name':'gmail_send_draft','arguments':{'draft_id':'fixture'}}]:
        script = SCRIPT.replace("{'name':'document_ask','arguments':{'document_ref':ref,'question':u['user_text']}}", repr(call))
        async with world(script=script) as w:
            await C.first(w, TEXT); await C.notice(w, 'no-authority-from-document')
            require_equal(len(w.native_launches), 2)
            require_equal(w.document_calls, [])
            require_equal(len(w.ledger.recent_runs()), 1)
            require_equal(await w.store.list_pending(), [])
            await C.H.end(w, w.client)


async def t_closed_or_corrected_conversation_does_not_read_attachment():
    async with world() as w:
        await C.first(w, TEXT)
        require(w.session._read_continuation is not None)
        await C.H.end(w, w.client)
        require(w.session._read_continuation is None)
        require_equal(w.document_calls, [])
    async with world() as w:
        await C.first(w, TEXT)
        await w.provider.events.put(C.H.transcript('Nein, lass es.',1000,1400,'correction'))
        await C.H.until(lambda:bool(w.session._input_parts))
        require(w.session._read_continuation is None)
        require_equal(w.document_calls, [])
        await C.H.end(w,w.client)


def t_document_private_snapshot_scrubs_history_and_disallows_room_continuation():
    tools = [DocumentCapabilityTool(name,None,None).schema() for name in D.SPECS]
    rows = [{'name':'document_find','arguments':{'query':'Rechnung'},'result':{'success':True,
        'data':{'document_ref':D._ref(subject='Dein Einmalcode lautet: 123456')}}}]
    with B.world() as (_,_,backend,_):
        snap=B.snapshot(backend, tools=[*tools,*B.TOOLS],core_results=rows,
            history=[{'role':'assistant','content':'Dein Einmalcode lautet: 123456'}])
        require('123456' not in snap.input_json)
        require_equal({t['name'] for t in json.loads(snap.tools_json)}, set(D.SPECS))
        B.raises(lambda:B.snapshot(backend, source=B.source('voice_room'),tools=tools,core_results=rows))


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
