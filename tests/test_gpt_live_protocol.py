"""Documented Live envelopes and adversarial values, no socket or provider."""
import base64
from dataclasses import FrozenInstanceError
import importlib.util
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from _guard import enforce_assertions, require, require_equal, require_raises
enforce_assertions()
# This pure contract module needs no installed SDK. Importing the existing
# realtime package first would import its unrelated transport dependencies.
# Load the exact source, without substituting any provider or module behavior.
_spec = importlib.util.spec_from_file_location(
    'solvio_live_protocol_contract',
    Path(__file__).resolve().parent.parent / 'src/solvio/realtime/live_protocol.py')
L = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = L
_spec.loader.exec_module(L)


def _transcript(**changes):
    return {"type":"session.input_transcript.delta", "event_id":"evt_in_1",
            "delta":"Die mit dem Fahrrad.", "start_ms":1000, "end_ms":1800, **changes}


def _usage(**changes):
    return {"type":"session.usage.updated", "event_id":"evt_usage_1",
            "usage":{"seconds":12.5}, **changes}


def _closed(**changes):
    return {"type":"session.closed", "event_id":"evt_closed_1",
            "client_event_id":"evt_close_1", "reason":"close_requested",
            "session":{"id":"live_abc123", "model":"gpt-live-1", "status":"active",
                       "expires_at":1788555600}, "usage":{"seconds":45.8}, **changes}


def _delegation(**changes):
    return {"type":"session.delegation.created", "event_id":"evt_delegate_1", "offset_ms":1800,
            "delegation":{"id":"opaque:item.1", "type":"delegation", "target":"client"}, **changes}


def t_session_start_is_client_only_raw16k_and_supports_existing_cedar():
    start = json.loads(json.dumps(L.session_start('cedar','Du bist SOLVIO.')))
    require_equal(L.LIVE_URL, 'wss://api.openai.com/v1/live/sessions')
    require_equal(start, {"type":"session.start", "session":{
        "model":"gpt-live-1", "instructions":"Du bist SOLVIO.",
        "audio":{"format":{"type":"audio/pcm","rate":16000},"output":{"voice":"cedar"}},
        "delegation":{"type":"client"}, "store":False}})
    require_equal(L.OUTPUT_RATE,16000)
    for voice in ('marin','quartz','willow'):
        require_equal(L.session_start(voice,'Hallo')['session']['audio']['output']['voice'],voice)
    for voice in ('gpt-realtime-2.1','voice_unknown',{'id':'voice_123'},None):
        require_raises(L.ProtocolError,L.session_start,voice,'Hallo')
    for instructions in ('',{},None,'x'*(L.MAX_INSTRUCTIONS_UTF8_BYTES+1),'bad\ud800'):
        require_raises(L.ProtocolError,L.session_start,'cedar',instructions)


def t_pcm_known_wire_bytes_round_trip_without_turn_or_container_commands():
    # Documented base64 example, complete PCM16 samples with signed extrema.
    encoded = 'AACAAIAAAIAAAP9/AIAAgA=='
    samples = base64.b64decode(encoded)
    require_equal(L.audio_append(samples),{'type':'session.input_audio.append','audio':encoded})
    require_equal(L.decode_audio({'type':'session.output_audio.delta','delta':encoded}),samples)
    require_equal(L.audio_append(b'\x00\x00')['audio'],'AAA=')
    require_equal(L.decode_audio({'type':'response.output_audio.delta','delta':encoded}),None)
    require_equal(L.decode_audio({'type':'session.output_audio.delta','delta':''}),b'')


def t_audio_rejects_malformed_base64_odd_pcm_and_unbounded_frames():
    for pcm in (b'\x01','AAA=',bytearray(b'\x00\x00'),b'\0'*(L.MAX_AUDIO_BYTES+2)):
        require_raises(L.ProtocolError,L.audio_append,pcm)
    for value in ('!!!!','AQ==','AA A=',None,{},'äAAA','a'*(4*((L.MAX_AUDIO_BYTES+2)//3)+4)):
        require_raises(L.ProtocolError,L.decode_audio,{'type':'session.output_audio.delta','delta':value})


def t_context_appends_preserve_nullable_delegation_and_exact_plain_text():
    for kind in ('instructions','thinking','commentary'):
        for delegation in (None,'opaque:item.1'):
            text=' Noch nicht gespeichert.\nBitte warten. '
            require_equal(L.append_update(kind,text,delegation,'event_1'),{
                'type':'session.'+kind+'.append','content':text,
                'delegation_id':delegation,'event_id':'event_1'})
    require_equal(len(L.append_update('thinking','a'*500,None,'id')['content']),500)
    # UTF-8, not the unsafe "four characters per token" estimate.
    require_equal(L.append_update('thinking','ü'*250,None,'id')['content'],'ü'*250)
    for content in ('a'*501,'ü'*251,'😀'*126,{},None,'bad\x00value','bad\ud800'):
        exc = require_raises(L.ProtocolError,L.append_update,'thinking',content,None,'id')
        require('bad' not in str(exc))
    for kind in ('response.create','function_call','session.commentary.append',None,{}):
        require_raises(L.ProtocolError,L.append_update,kind,'Test',None,'id')
    for ident in ('','\nprivate','x'*513,{}):
        require_raises(L.ProtocolError,L.append_update,'thinking','Test',ident,'id')
        require_raises(L.ProtocolError,L.append_update,'thinking','Test',None,ident)


def t_transcripts_preserve_overlap_repetition_whitespace_and_delivery_order():
    a=L.parse_transcript(_transcript(delta=' Die ',start_ms=1000,end_ms=1200))
    b=L.parse_transcript(_transcript(type='session.output_transcript.delta',delta='Ja.',start_ms=1100,end_ms=1300))
    c=L.parse_transcript(_transcript(delta='mit dem Fahrrad. Fahrrad.',start_ms=1200,end_ms=1800))
    require_equal(a.role,'user'); require_equal(b.role,'assistant')
    require_equal(a.delta+c.delta,' Die mit dem Fahrrad. Fahrrad.')
    require(a.start_ms < b.start_ms < a.end_ms)
    require_equal(L.parse_transcript(_transcript(start_ms=0,end_ms=0,delta='')).delta,'')
    require_equal(L.parse_transcript({'type':'conversation.item.input_audio_transcription.completed'}),None)
    require_raises(FrozenInstanceError,setattr,a,'delta','changed')


def t_transcript_rejects_nontext_nonfinite_reversed_and_boolean_timing():
    for field in ('start_ms','end_ms'):
        for value in (True,False,-1,float('inf'),float('nan'),'1000',None,10**1000):
            require_raises(L.ProtocolError,L.parse_transcript,_transcript(**{field:value}))
    require_raises(L.ProtocolError,L.parse_transcript,_transcript(start_ms=2000,end_ms=1000))
    for value in ({'text':'execute'},['hello'],None,'bad\ud800'):
        require_raises(L.ProtocolError,L.parse_transcript,_transcript(delta=value))
    bad=_transcript(); del bad['event_id']
    require_raises(L.ProtocolError,L.parse_transcript,bad)


def t_client_delegation_is_metadata_not_tool_arguments_or_authority():
    item=_delegation(task='forged',arguments={'scope':'build'},principal='claimed-owner')
    parsed=L.parse_delegation(item)
    require_equal(parsed.delegation_id,'opaque:item.1'); require_equal(parsed.offset_ms,1800)
    require(not hasattr(parsed,'task') and not hasattr(parsed,'principal') and not hasattr(parsed,'arguments'))
    require_equal(L.parse_delegation({'type':'response.event'}),None)
    for target in ('responses','tool',None):
        require_raises(L.ProtocolError,L.parse_delegation,_delegation(
            delegation={'id':'item_1','type':'delegation','target':target}))
    for value in ({},None,{'id':'item_1','target':'client'}, {'id':'','type':'delegation','target':'client'}):
        require_raises(L.ProtocolError,L.parse_delegation,_delegation(delegation=value))
    require_raises(L.ProtocolError,L.parse_delegation,_delegation(offset_ms=float('nan')))


def t_usage_is_cumulative_and_final_only_for_valid_session_closed():
    first=L.parse_usage(_usage())
    second=L.parse_usage(_usage(usage={'seconds':20},context_window={'usage_ratio':0.42}))
    final=L.parse_usage(_closed())
    require_equal((first.seconds,second.seconds,final.seconds),(12.5,20,45.8))
    require_equal((first.final,second.final,final.final),(False,False,True))
    require_equal(final.session_id,'live_abc123'); require_equal(final.reason,'close_requested')
    # Official final snapshot status is active: never wait for status=closed.
    require_equal(_closed()['session']['status'],'active')
    for reason in ('expired','content','remote_hangup','connection_lost'):
        result=L.parse_usage(_closed(reason=reason))
        require(result.final); require_equal(result.reason,reason)
    for kind in ('websocket.closed','response.done','session.started'):
        require_equal(L.parse_usage({'type':kind,'usage':{'seconds':0}}),None)


def t_missing_or_invalid_usage_never_becomes_zero_or_confirmed_final():
    for value in ({},None,{'seconds':True},{'seconds':-1},{'seconds':float('nan')},
                  {'seconds':float('inf')},{'seconds':'0'},{'seconds':10**1000}):
        require_raises(L.ProtocolError,L.parse_usage,_usage(usage=value))
        require_raises(L.ProtocolError,L.parse_usage,_closed(usage=value))
    for value in ({},None,{'id':''}):
        require_raises(L.ProtocolError,L.parse_usage,_closed(session=value))
    for reason in ('success','',None,{}):
        require_raises(L.ProtocolError,L.parse_usage,_closed(reason=reason))
    require_equal(L.parse_usage(_usage(usage={'seconds':0})).seconds,0)


def t_parsers_reject_nonobject_and_missing_types_without_private_errors():
    for parser in (L.parse_transcript,L.decode_audio,L.parse_delegation,L.parse_usage):
        for value in ('private transcript',[],None,{}, {'type':{}}):
            exc=require_raises(L.ProtocolError,parser,value)
            require_equal(str(exc),'invalid_event')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
