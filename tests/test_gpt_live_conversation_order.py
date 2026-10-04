"""Observed Live transcript order in the existing authenticated conversation store."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_session as W


def rows(w):
    return [(row["role"], row["text"]) for row in
            w.server.conversations.recent_context(w.sessions[0].conversation_id)]


async def emit(w, role, text, index):
    await w.providers[0].events.put(W.transcript(text, index * 1000, (index + 1) * 1000,
        f"part-{index}", role="input" if role == "user" else "output"))
    await W.until(lambda: f"part-{index}" in w.sessions[0]._seen_fragments)


async def t_two_observed_exchanges_keep_role_order_after_close():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        session = w.sessions[0]
        session._offer_adaptive = Mock(wraps=session._offer_adaptive)
        expected = [("user", "Wie wird das Wetter?"), ("assistant", "Morgen wird es sonnig."),
                    ("user", "Und am Wochenende?"), ("assistant", "Dann regnet es.")]
        for i, (role, text) in enumerate(expected):
            await emit(w, role, text, i)
        await W.end(w, ws)
        require_equal(rows(w), expected)
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        require_equal(session._offer_adaptive.call_count, 0)


async def t_split_deltas_and_duplicate_events_remain_once_in_reconnect_history():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        fragments = [("user", "Hallo, "), ("user", "bist du da?"),
                     ("assistant", "Ja, "), ("assistant", "ich bin da."),
                     ("user", "Danke."), ("assistant", "Gern.")]
        for i, (role, text) in enumerate(fragments):
            await emit(w, role, text, i)
        # A replay arriving after later exchanges must not reopen an old group.
        await w.providers[0].events.put(W.transcript("Hallo, ", 0, 1000, "part-0"))
        await w.providers[0].events.put({"type": "session.usage.updated", "event_id": "reconnect",
                                       "usage": {"seconds": "malformed"}})
        await W.until(lambda: len(w.providers) == 2 and w.sessions[0].active)
        expected = [("user", "Hallo, bist du da?"), ("assistant", "Ja, ich bin da."),
                    ("user", "Danke."), ("assistant", "Gern.")]
        require_equal(rows(w), expected)
        require_equal([(row["role"], row["content"][0]["text"])
                       for row in w.providers[1].started["input"]], expected)
        await W.end(w, ws)
        require_equal(rows(w), expected)


async def t_delegation_after_two_exchanges_reuses_current_original_source_once():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        expected = [("user", "Hallo."), ("assistant", "Ich bin da."),
                    ("user", W.TEXT), ("assistant", "Ich prüfe das.")]
        for i, (role, text) in enumerate(expected):
            await emit(w, role, text, i)
        session = w.sessions[0]
        await asyncio.wait_for(session._persist_queue.join(), 1)
        original = w.server.conversations.messages(session.conversation_id)[-1]
        require_equal(original["text"], W.TEXT)
        await w.providers[0].events.put(W.delegate(offset=3000))
        await W.until(lambda: len(w.ledger.recent_runs()) == 1)
        await asyncio.wait_for(session._tool_queue.join(), 1)
        snapshot = w.selector.calls[0]
        require_equal(snapshot["user_text"], W.TEXT)
        require_equal(snapshot["source"].message_id, original["message_id"])
        require(await snapshot["source_current"]())
        require_equal(original["source_session_id"], session.session_id)
        require_equal(snapshot["history"], [{"role": role, "content": text}
            for role, text in (expected[0], expected[1], expected[3])])
        await W.end(w, ws)
        require_equal(rows(w), expected)
        require_equal(len(w.selector.calls), 1)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_delegated_input_is_not_duplicated_by_later_observed_exchanges():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        await emit(w, "user", W.TEXT, 0)
        await w.providers[0].events.put(W.delegate())
        await W.until(lambda: len(w.ledger.recent_runs()) == 1)
        await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 1)
        snapshot = w.selector.calls[0]
        require(await snapshot["source_current"]())
        rest = [("assistant", "Der Auftrag läuft."), ("user", "Danke."), ("assistant", "Gern.")]
        for i, (role, text) in enumerate(rest, start=1):
            await emit(w, role, text, i)
        require(not await snapshot["source_current"]())
        await W.end(w, ws)
        require_equal(rows(w), [("user", W.TEXT)] + rest)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_correction_invalidates_old_source_and_fresh_callback_uses_only_new_original():
    async with W.world() as w:
        w.selector.release = asyncio.Event()
        w.selector.selection = W.LiveSelection(clarification="Bitte präzisiere den Auftrag.")
        ws, _ = await W.H.started(w)
        await emit(w, "user", W.TEXT, 0)
        await w.providers[0].events.put(W.delegate())
        await asyncio.wait_for(w.selector.entered.wait(), 1)
        old = w.selector.calls[0]
        correction = "Nein, noch nicht starten."
        await emit(w, "user", correction, 1)
        require(not await old["source_current"]())
        w.selector.release.set()
        await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 1)
        await w.providers[0].events.put(W.delegate("corrected", offset=2000))
        await W.until(lambda: len(w.selector.calls) == 2)
        await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 1)
        new = w.selector.calls[1]
        require_equal(new["user_text"], correction)
        require(new["source"].message_id != old["source"].message_id)
        require(await new["source_current"]())
        require_equal(new["history"], [{"role": "user", "content": W.TEXT}])
        await W.end(w, ws)
        require_equal(rows(w), [("user", W.TEXT), ("user", correction)])
        require_equal(w.ledger.recent_runs(), [])


async def t_late_input_after_recorded_exchange_cannot_gain_delegation_authority():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        await emit(w, "user", "Hallo.", 0)
        await emit(w, "assistant", "Guten Tag.", 1)
        await w.providers[0].events.put(W.transcript(W.TEXT, 500, 900, "late-input"))
        await w.providers[0].events.put(W.delegate())
        await W.until(lambda: any(e["type"] == "session.commentary.append" for e in w.providers[0].sent))
        await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 1)
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        await W.end(w, ws)
        require_equal(rows(w), [("user", "Hallo."), ("assistant", "Guten Tag."), ("user", W.TEXT)])


async def t_empty_other_role_deltas_do_not_split_unfinished_words_or_original_source():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        fragments = [("user", "Guten"), ("assistant", ""), ("assistant", " \n"),
                     ("user", " "), ("user", "Tag."), ("assistant", "Hallo"),
                     ("user", ""), ("user", " \n"), ("assistant", " "), ("assistant", "da!")]
        for i, (role, text) in enumerate(fragments):
            await emit(w, role, text, i)
        await W.end(w, ws)
        require_equal(rows(w), [("user", "Guten Tag."), ("assistant", "Hallo da!")])
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
