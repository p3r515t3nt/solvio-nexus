"""Unanswered speech recovery uses provider instructions, never invented authority."""
import asyncio
from unittest.mock import patch
import test_gpt_live_session as W
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

def nudges(provider):
    return [e for e in provider.sent if e['type']=='session.instructions.append']

async def t_silent_provider_is_nudged_once_and_real_notice_starts_original_once():
    with patch.object(W.L,'UNANSWERED_NUDGE_SECONDS',.06,create=True):
      async with W.world() as w:
        ws,_=await W.H.started(w);p=w.providers[0];s=w.sessions[0]
        await p.events.put(W.transcript())
        await W.until(lambda:bool(nudges(p)))
        require_equal(w.selector.calls,[]);require_equal(w.ledger.recent_runs(),[])
        require_equal(nudges(p)[0]['delegation_id'],None)
        await asyncio.sleep(.1);require_equal(len(nudges(p)),1)
        await p.events.put(W.delegate('real-after-nudge'))
        await W.until(lambda:len(w.ledger.recent_runs())==1);await s._tool_queue.join()
        require_equal(w.selector.calls[0]['user_text'],W.TEXT)
        await p.events.put(W.delegate('real-after-nudge'));await asyncio.sleep(.1)
        require_equal(len(w.selector.calls),1);require_equal(len(w.ledger.recent_runs()),1)
        await W.end(w,ws)

async def t_still_silent_provider_keeps_text_and_honest_failure_without_any_task():
    with patch.object(W.L,'UNANSWERED_NUDGE_SECONDS',.05,create=True):
      async with W.world() as w:
        w.server.idle_timeout=.25
        ws,_=await W.H.started(w);p=w.providers[0];s=w.sessions[0]
        await p.events.put(W.transcript())
        await W.H.receive(ws,'session_closed');await W.H.closed(ws)
        rows=w.server.conversations.recent_context(s.conversation_id)
        require_equal([r['text'] for r in rows if r['role']=='user'],[W.TEXT])
        require(any(r['role']=='assistant' and 'Sprachdienst' in r['text'] and 'nicht' in r['text'] for r in rows))
        require_equal(len(nudges(p)),1)
        require_equal(w.selector.calls,[]);require_equal(w.ledger.recent_runs(),[]);require(p.closed)

async def t_ordinary_answer_and_inflight_selection_are_not_nudged():
    for mode in ('answer','selection'):
      with patch.object(W.L,'UNANSWERED_NUDGE_SECONDS',.05,create=True):
       async with W.world() as w:
        ws,_=await W.H.started(w);p=w.providers[0]
        await p.events.put(W.transcript())
        if mode=='answer':await p.events.put(W.transcript('Welche Angaben brauchst du?',1100,1800,'out',role='output'))
        else:
            w.selector.release=asyncio.Event()
            await p.events.put(W.delegate());await w.selector.entered.wait()
        await asyncio.sleep(.15);require_equal(nudges(p),[])
        if mode=='selection':w.selector.release.set();await w.sessions[0]._tool_queue.join()
        await W.end(w,ws)

async def t_correction_after_nudge_is_the_only_original_for_later_real_notice():
    with patch.object(W.L,'UNANSWERED_NUDGE_SECONDS',.05,create=True):
      async with W.world() as w:
        ws,_=await W.H.started(w);p=w.providers[0];s=w.sessions[0]
        await p.events.put(W.transcript());await W.until(lambda:bool(nudges(p)))
        correction=' Nein, nur prüfen und nichts starten.'
        w.selector.selection=W.LiveSelection(clarification='Was genau soll ich prüfen?')
        await p.events.put(W.transcript(correction,1000,2000,'correction'))
        await p.events.put(W.delegate('after-correction',2000))
        await W.until(lambda:bool(w.selector.calls));await s._tool_queue.join()
        require_equal(w.selector.calls[0]['user_text'],W.TEXT+correction)
        require_equal(w.ledger.recent_runs(),[]);require_equal(len(nudges(p)),1)
        await W.end(w,ws)

if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
