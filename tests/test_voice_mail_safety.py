"""Bounded review regressions: turn authority and truthful voice mail results.

The existing in-memory Gmail/real approval-chain harness and transcript fixture are
reused. No provider, account, device, mailbox or production state is contacted.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

import test_voice_mail_actions as voice  # noqa: E402


async def t_another_sessions_confirmed_send_does_not_back_this_sessions_claim():
    sender = voice._session()
    listener = voice._session()
    sender.session_id, listener.session_id = "s-sender", "s-unrelated"
    # The actual Orchestrator currently retains only this process-wide timestamp.
    # It belongs to the sender; the listener has neither a mail action nor a receipt.
    listener.runtime = sender.runtime
    listener.server.dispatcher.agent_runtime = sender.runtime
    sender.runtime.confirmed_mail_sends.append(time.time())
    listener.say("user", "Was steht in meiner Rechnung?")
    listener.say("assistant", "Ich habe die Mail weitergeleitet.")
    await voice._settle(listener)
    require_equal(len(voice._corrections(listener)), 1,
                  "an unrelated session's send must not silence this claim correction")


async def _denied_turn_retry(*, change_action):
    from solvio.security.mobile_approval import protocol as PR
    s = await voice._stack(voice._gate(
        "Schreib gregor minus brawanski at web punkt test: Ich komme um acht."))
    try:
        context = s.gate.context()
        args = {"to": voice.WEB, "subject": "Heute", "body": "Ich komme um acht."}
        tool = s.dispatcher.tool("mail_send")
        first = await tool.run(args)
        require_equal(first.error, "approval_required")
        wire, reason = await s.cp.issue_challenge(
            approval_id=first.data["request_id"], device_id=s.device.device_id)
        require(wire is not None, f"synthetic denial challenge failed: {reason}")
        s.counter[0] += 1
        _result, status = await s.co.apply_mobile_decision(**s.H.sign_decision(
            s.device, PR.b64d(wire["payload_b64"]), decision=PR.DECISION_DENY,
            counter=s.counter[0]))
        require_equal(status, "ok")
        require_equal(await s.storage.list_pending(), [], "the first decision is terminal")
        if change_action:
            args = {**args, "subject": "Anders", "body": "Ich komme um neun."}
        second = await tool.run(args)
        require(s.gate.context() is context, "the fixture must not supply a new user turn")
        require_equal(s.api.sent, [], "no mail may leave during denial or retry")
        require_equal(await s.storage.list_pending(), [],
                      "a denied authenticated turn must not create another Face ID request")
        require(not second.success and second.error != "approval_required",
                "retry must remain refused until a new authenticated user instruction")
    finally:
        await s.storage.close()


async def t_same_mail_same_turn_after_denial_does_not_create_a_new_approval():
    await _denied_turn_retry(change_action=False)


async def t_changed_mail_same_turn_after_denial_does_not_create_a_new_approval():
    # A changed model argument is not a fresh owner instruction. A genuinely new
    # user turn remains a separate action and is deliberately outside this refusal.
    await _denied_turn_retry(change_action=True)


async def t_reconstructed_same_turn_cannot_switch_from_send_to_forward():
    s = await voice._stack()
    try:
        tool = s.dispatcher.tool("mail_send")
        first = await tool.run({"to": voice.WEB, "subject": "Heute", "body": "Hallo."})
        require_equal(first.error, "approval_required")
        old = s.gate.context()
        s.gate.begin_turn(session_id=old.session_id, turn_id=old.turn_id,
                          principal=old.principal, trust=old.trust,
                          user_text=old.user_text, origin=old.origin)
        require(s.gate.context() is not old, "Core reconstruction must use a new object")
        second = await s.dispatcher.tool("mail_forward").run(
            {"query": "from:elevenlabs", "to": voice.WEB})
        require_equal(second.error, "mail_turn_already_used")
        require_equal(len(await s.storage.list_pending()), 1)
        require_equal(len(s.api.drafts), 1)
    finally:
        await s.storage.close()


async def t_bound_receipt_expires_on_new_user_input_and_never_crosses_principals():
    from types import SimpleNamespace
    s = voice._session()
    s.say("user", "Schreib diese Mail.")
    s._tool_results = [{"error": "approval_required", "data": {"request_id": "ap-bound"}}]
    s._remember_mail_request([{"name": "mail_send"}], SimpleNamespace(principal="owner"))
    s.runtime.confirmed_mail_sends.append({"request_id": "ap-bound", "principal": "other", "at": time.time()})
    require(not s._core_confirmed_send(), "another principal's receipt cannot count")
    s.runtime.confirmed_mail_sends.append({"request_id": "ap-bound", "principal": "owner", "at": time.time()})
    require(s._core_confirmed_send(), "the exact current request must count")
    s.say("user", "Und die andere Mail?")
    s.say("assistant", "Die Mail ist verschickt.")
    await voice._settle(s)
    require_equal(len(voice._corrections(s)), 1, "a previous turn's receipt must not cover a new request")


async def _stale_after_await(stage):
    observations = []
    for replace in (False, True):
        s = await voice._stack()
        reached, release = asyncio.Event(), asyncio.Event()
        original = s.api._request
        target = ("GET", "/messages") if stage == "search" else ("POST", "/drafts")

        async def held_request(method, path, **kw):
            result = await original(method, path, **kw)
            if (method, path) == target:
                reached.set()
                await release.wait()
            return result

        s.api._request = held_request
        running = asyncio.create_task(s.dispatcher.tool("mail_forward").run(
            {"query": "from:elevenlabs receipt", "to": voice.WEB}))
        try:
            await asyncio.wait_for(reached.wait(), 2)
            context = s.gate.context()
            s.gate.clear()  # LiveSession clears the gate when new user input arrives.
            if replace:
                # Same identity and words, but a different authenticated turn:
                # provenance similarity must not make the old invocation current.
                s.gate.begin_turn(session_id=context.session_id, turn_id="s-voice-t2",
                                  principal=context.principal, trust=context.trust,
                                  user_text=context.user_text, origin=context.origin)
            release.set()
            result = await asyncio.wait_for(running, 2)
            observations.append({
                "replacement_turn": replace,
                "pending": len(await s.storage.list_pending()),
                "drafts": len(s.api.drafts), "sent": len(s.api.sent),
                "success": result.success,
                "approval_required": result.error == "approval_required",
            })
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await s.storage.close()
    # Draft creation that completed before interruption cannot be undone by this
    # guard; what matters is that no subsequent effect or approval is initiated.
    allowed_drafts = 0 if stage == "search" else 1
    require(all(row["pending"] == row["sent"] == 0
                and row["drafts"] <= allowed_drafts
                and not row["success"] and not row["approval_required"]
                for row in observations),
            f"stale {stage} continuation crossed the original turn: {observations}")


async def t_turn_clear_or_replacement_during_search_stops_mail_preparation():
    await _stale_after_await("search")


async def t_turn_clear_or_replacement_during_draft_stops_approval_creation():
    await _stale_after_await("draft")


async def t_second_refused_mail_in_a_batch_keeps_the_first_request_receipt():
    s = await voice._stack()
    try:
        first = await s.dispatcher.tool("mail_send").run(
            {"to": voice.WEB, "subject": "Heute", "body": "Hallo."})
        second = await s.dispatcher.tool("mail_forward").run(
            {"query": "from:elevenlabs", "to": voice.WEB})
        require_equal(first.error, "approval_required")
        require_equal(second.error, "mail_turn_already_used")
        live = voice._session()
        live._tool_results = [first.as_dict(), second.as_dict()]
        live._remember_mail_request([{"name": "mail_send"}, {"name": "mail_forward"}], s.gate.context())
        live.runtime.confirmed_mail_sends.append({"request_id": first.data["request_id"],
                "principal": s.gate.context().principal, "at": time.time()})
        require(live._core_confirmed_send(), "a refused second call erased the valid first request")
    finally:
        await s.storage.close()


async def t_turn_change_during_final_draft_description_creates_no_approval():
    for replace in (False, True):
        s = await voice._stack()
        try:
            def interrupt(method, path):
                if method == "GET" and path.startswith("/drafts/"):
                    old = s.gate.context()
                    s.gate.clear()
                    if replace:
                        s.gate.begin_turn(session_id=old.session_id, turn_id="new-turn",
                            principal=old.principal, trust=old.trust,
                            user_text=old.user_text, origin=old.origin)
            s.api.before_response = interrupt
            result = await s.dispatcher.tool("mail_send").run(
                {"to": voice.WEB, "subject": "Heute", "body": "Hallo."})
            require_equal(await s.storage.list_pending(), [],
                          "description must not request Face ID for an abandoned turn")
            require(not result.success and result.error != "approval_required")
            require_equal(s.api.sent, [])
        finally:
            await s.storage.close()


async def t_turn_lost_while_approval_is_stored_withdraws_the_new_request():
    s = await voice._stack()
    try:
        original = s.router._mobile.request
        async def interrupted(*args, **kwargs):
            request_id = await original(*args, **kwargs)
            s.gate.clear()
            return request_id
        s.router._mobile.request = interrupted
        result = await s.dispatcher.tool("mail_send").run(
            {"to": voice.WEB, "subject": "Heute", "body": "Hallo."})
        require_equal(await s.storage.list_pending(), [])
        require(not result.success and result.error.startswith("cancelled"))
        require_equal(s.api.sent, [])
    finally:
        await s.storage.close()


async def t_failing_source_liveness_check_never_prepares_or_requests_a_mail():
    s = await voice._stack()
    try:
        context = s.gate.context()
        def unavailable():
            raise RuntimeError("synthetic source unavailable")
        result = await s.router.execute("gmail_create_draft",
            {"to": voice.WEB, "subject": "Heute", "body": "Hallo."},
            trust=context.trust, principal=context.principal, origin=context.origin,
            commanded=True, invocation_current=unavailable)
        require_equal(result.outcome.value, "cancelled")
        require_equal(s.api.drafts, {})
        require_equal(await s.storage.list_pending(), [])
    finally:
        await s.storage.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
