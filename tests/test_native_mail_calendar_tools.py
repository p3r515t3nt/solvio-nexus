"""Stufe S1 (Kurskorrektur 25.09.2026): the task worker reads mail and calendar.

Driven through the real temporary Core: task grant, CapabilityRouter, cost
dispatch, native tool bridge and the ORIGINAL GmailCapabilities handler over a
recording fake provider. Nothing here talks to Google.
"""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_native_core_tools as C
from solvio.agent_runtime import cost_dispatch as D, native_sessions as N, native_tools as T
from solvio.capabilities import calendar as CAL, gmail as G, task_read as TR
from solvio.integrations.gmail import EmailMessage
from solvio.secret_vault import firewall

OWN = "owner@example.invalid"
SEARCH = {"threadId": "native-thread", "turnId": "native-turn", "callId": "call-mail",
          "tool": "gmail_search", "arguments": {"query": "rechnung", "limit": 5}}


class RecordingGmail:
    """The provider seam of the real GmailCapabilities; records every call."""

    def __init__(self, messages):
        self.messages = {m.message_id: m for m in messages}
        self.calls = []

    async def profile(self):
        return {"emailAddress": OWN}

    async def search(self, query="", *, limit=10, label=""):
        self.calls.append(("search", query, limit, label))
        return [m for m in self.messages.values() if query.lower() in (m.subject + m.body).lower()][:limit]

    async def message(self, message_id):
        self.calls.append(("message", message_id))
        return self.messages.get(message_id)

    async def thread(self, thread_id):
        self.calls.append(("thread", thread_id))
        return [m for m in self.messages.values() if m.thread_id == thread_id]


def mail(message_id, subject, body):
    return EmailMessage(message_id, "t-" + message_id, sender="Rechnungsstelle <buchhaltung@example.invalid>",
                        to=OWN, subject=subject, date="Thu, 24 Sep 2026 09:00 +0200", body=body,
                        labels=("INBOX",))


def mail_world(messages, names=("gmail_search", "gmail_read_message")):
    world = C.tools_world(names=names, register=False)
    w = world.__enter__()
    w.provider = RecordingGmail(messages)
    G.register(w.router, G.GmailCapabilities(w.provider))
    w._world = world
    return w


def close(w):
    w._world.__exit__(None, None, None)


async def t_a_mail_search_runs_once_through_the_real_router_with_a_settled_zero_cost_row():
    w = mail_world([mail("m1", "Rechnung September", "Betrag 120 Euro, faellig am 30.09.")])
    try:
        manifest = {tool["name"]: tool for tool in w.tools.manifest()}
        require_equal(sorted(manifest), ["gmail_read_message", "gmail_search"])
        require_equal(manifest["gmail_search"]["inputSchema"]["required"], ["query"])
        require(manifest["gmail_search"]["inputSchema"]["additionalProperties"] is False)

        async def native():
            reply = await w.tools.call(SEARCH)
            require(reply["success"], reply)
            body = C.payload(reply)
            require_equal(body["arguments"], {"query": "rechnung", "limit": 5})
            require_equal([m["subject"] for m in body["data"]["messages"]], ["Rechnung September"])
            require_equal(w.provider.calls, [("search", "rechnung", 5, "")])
            rows = C.tool_rows(w)
            require_equal([(r["call_id"], r["state"]) for r in rows], [("call-mail", "completed")])
            step = w.ledger.get_step(rows[0]["step_id"])
            require_equal((step.capability, step.state), ("gmail_search", "succeeded"))
            mail_costs = [c for c in D.invocations(w.ledger, w.task) if c["provider"] == "google.gmail"]
            require_equal([(c["operation_id"], c["state"]) for c in mail_costs], [(step.step_id, "finished")])
            # A fresh adapter replays the stored reply; the provider is not asked again.
            reopened = T.NativeCoreTools(w.ledger, N.NativeSessions(w.ledger, authority=w.authority),
                                         w.session.session_id, w.run, w.router)
            require_equal(await reopened.call(SEARCH), reply)
            require_equal(len(w.provider.calls), 1)
            # The same callId with other arguments never executes again.
            changed = await w.tools.call(dict(SEARCH, arguments={"query": "mahnung", "limit": 5}))
            require_equal(C.payload(changed), {"error": "native_tool_request_changed"})
            require_equal(len(w.provider.calls), 1)
        result = await C.dispatch(w, native)
        require_equal(result.cost_status, "settled")
        require_equal(w.costs.view(w.task)["ai_tool"]["spent_cents"], 0)
    finally:
        close(w)


async def t_arguments_are_strict_and_nothing_foreign_reaches_the_provider():
    w = mail_world([mail("m1", "Rechnung", "x")])
    try:
        bad = [{"query": ""}, {"query": "rechnung", "limit": 0}, {"query": "rechnung", "limit": 26},
               {"query": "rechnung", "label": "SENT"}, {"limit": 5}, {"query": 7},
               {"query": "rechnung", "limit": True}, {"query": "x" * 301},
               # A query that itself carries a key would be echoed in the stored reply.
               {"query": "sk-" + "A1b2C3d4E5f6G7h8I9j0"}]
        for arguments in bad:
            try:
                T._request(dict(SEARCH, arguments=arguments))
            except ValueError as exc:
                require_equal(str(exc), "native_tool_request_invalid", arguments)
            else:
                raise AssertionError("accepted: " + json.dumps(arguments))
        for message_id in ("../m1", "m1 m2", "", "m" * 129):
            try:
                T._request(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": message_id}))
            except ValueError:
                pass
            else:
                raise AssertionError("accepted message id: " + message_id)
        # The write tools of the same service are never task tools.
        for tool in ("gmail_create_draft", "gmail_send_draft", "calendar_create_event"):
            try:
                T._request(dict(SEARCH, tool=tool, arguments={}))
            except ValueError:
                pass
            else:
                raise AssertionError("write tool accepted: " + tool)

        async def native():
            for arguments in bad[:3]:
                require((await w.tools.call(dict(SEARCH, arguments=arguments)))["success"] is False)
            require_equal(w.provider.calls, [])
            require_equal(C.tool_rows(w), [])
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_an_ungranted_read_is_absent_from_the_manifest_and_refused():
    w = mail_world([mail("m1", "Rechnung", "x")], names=("gmail_search",))
    try:
        require_equal([tool["name"] for tool in w.tools.manifest()], ["gmail_search"])

        async def native():
            reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": "m1"}))
            require(reply["success"] is False, reply)
            require_equal(w.provider.calls, [])
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_credentials_in_a_mail_are_blacked_out_and_the_rest_stays_readable():
    key = "sk-" + "Zq8Wv7Ut6Sr5Qp4On3Ml2"
    jwt = "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".sig"
    body = ("Hallo, Ihre Rechnung ueber 120 Euro liegt bei.\n"
            "Anmelden: https://portal.example.invalid/login?token=" + jwt + "\n"
            "API-Schluessel " + key + " bitte nicht weitergeben.\n"
            "password = Sommer2024xyz\n"
            "Viele Gruesse")
    w = mail_world([mail("m1", "Rechnung", body)])
    try:
        async def native():
            reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": "m1"}))
            require(reply["success"], reply)
            body = C.payload(reply)["data"]["body"]
            require(type(body) is list, "a multi-line mail comes as a list of lines")
            text = "\n".join(body)
            require(key not in text and jwt not in text and "Sommer2024xyz" not in text, text)
            require(not firewall.has_key_shape(reply["contentItems"][0]["text"]))
            require("Ihre Rechnung ueber 120 Euro" in text and "Viele Gruesse" in text, text)
            # Only the key is blacked out, not its whole line: the context stays.
            require("bitte nicht weitergeben" in text, text)
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_a_long_thread_is_shortened_visibly_and_fits_the_reply_limit():
    long_body = "Absatz mit Inhalt. " * 5000
    messages = [EmailMessage("m%d" % i, "t-long", sender="a@example.invalid", to=OWN,
                             subject="Lang %d" % i, body=long_body, labels=("INBOX",)) for i in range(4)]
    w = mail_world(messages, names=("gmail_read_thread",))
    try:
        async def native():
            reply = await w.tools.call(dict(SEARCH, tool="gmail_read_thread", arguments={"thread_id": "t-long"}))
            require(reply["success"], reply)
            raw = json.dumps(reply, ensure_ascii=False)
            require(len(reply["contentItems"][0]["text"]) <= T.MAX_RESPONSE_CHARS)
            require("[gekuerzt]" in raw)
            require_equal(C.payload(reply)["data"]["count"], 4)
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_a_turn_reads_at_most_twelve_times_and_is_told_so():
    w = mail_world([mail("m1", "Rechnung", "x")], names=("gmail_read_message",))
    try:
        async def native():
            for index in range(T.MAX_READ_CALLS):
                reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", callId="c%d" % index,
                                                arguments={"message_id": "m1"}))
                require(reply["success"], reply)
            over = await w.tools.call(dict(SEARCH, tool="gmail_read_message", callId="c-over",
                                           arguments={"message_id": "m1"}))
            require_equal(C.payload(over), {"error": "native_tool_read_limit"})
            require_equal(len(w.provider.calls), T.MAX_READ_CALLS)
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_chat_and_voice_keep_the_unchanged_handler():
    provider = RecordingGmail([mail("m1", "Rechnung", "password = Sommer2024xyz")])
    capabilities = G.GmailCapabilities(provider)
    registered = {}

    class Router:
        def register(self, spec, handler, **_):
            registered[spec.name] = handler

    G.register(Router(), capabilities)
    require(type(registered["gmail_read_message"]) is G.GmailTaskRead)
    require(type(registered["gmail_send_draft"]) is not G.GmailTaskRead, "a write never gets the task wrapper")
    direct = await capabilities.read_message({"message_id": "m1"})
    via_chat = await registered["gmail_read_message"]({"message_id": "m1"})
    require_equal(via_chat, direct, "the chat path returns exactly the original handler's data")


def t_a_foreign_or_altered_handler_never_gets_the_task_contract():
    from solvio.capabilities.router import _task_service_route
    capabilities = G.GmailCapabilities(RecordingGmail([]))
    genuine = G.GmailTaskRead("gmail_search", capabilities.search)
    require_equal(_task_service_route(genuine), ("google.gmail", "read"))

    class Other:
        async def search(self, arguments):
            return {}
    for service in (G.GmailTaskRead("gmail_search", Other().search),
                    G.GmailTaskRead("gmail_search", capabilities.read_message),
                    G.GmailTaskRead("gmail_send_draft", capabilities.send_draft)):
        try:
            service.resources(G.SPECS[service.name], {"query": "x"})
        except (ValueError, Exception):
            pass
        else:
            raise AssertionError("foreign binding accepted")

    class Altered(G.GmailTaskRead):
        async def execute(self, arguments, task_step):
            return {}
    require(_task_service_route(Altered("gmail_search", capabilities.search)) is None)
    overridden = G.GmailTaskRead("gmail_search", capabilities.search)
    overridden.execute = lambda *_: {}
    require(_task_service_route(overridden) is None, "an instance override loses the route")
    overridden = G.GmailTaskRead("gmail_search", capabilities.search)
    overridden._binding = lambda: {"contract": TR.CONTRACT}
    try:
        overridden.resources(G.SPECS["gmail_search"], {"query": "x"})
    except Exception:
        raise AssertionError("the class binding must not be replaceable by an instance attribute")
    overridden.handler = Other().search
    try:
        overridden.resources(G.SPECS["gmail_search"], {"query": "x"})
    except ValueError:
        pass
    else:
        raise AssertionError("an instance `_binding` bypassed the handler check")


def t_calendar_reads_share_the_contract_and_their_own_route():
    from solvio.capabilities.router import _task_service_route
    require_equal(set(CAL.CalendarTaskRead.METHODS), {"calendar_list_events", "calendar_get_event",
                                                      "calendar_search_events", "calendar_find_availability"})
    require_equal(CAL.CalendarTaskRead.ROUTE, ("google.calendar", "read"))
    for name in CAL.CalendarTaskRead.METHODS:
        require(CAL.SPECS[name].is_read_only(), name)
        require_equal(T.ROUTES[name], "google.calendar")
        require(name in T.TOOLS and name in TR.ARGUMENT_RULES)
    for name in G.GmailTaskRead.METHODS:
        require(G.SPECS[name].is_read_only(), name)
        require_equal(T.ROUTES[name], "google.gmail")
    require(all(not name.endswith(("create_event", "update_event", "delete_event", "create_draft", "send_draft"))
                for name in T.TOOLS), "no write capability is a task tool")
    require(_task_service_route(object()) is None)


def t_only_the_chat_classifier_sends_mail_and_calendar_questions_to_a_task():
    """The chat answer has no tools; only the general task path reads mail and
    calendar. The rule lives in the chat-only addition: the voice path has its
    own tools and keeps its classifier text unchanged."""
    from solvio.cognition import prompt as P
    addition = P.TASK_PROFILE_INSTRUCTION
    require("Postfach" in addition and "Kalender" in addition, addition)
    require("`auftrag_recherche` mit `auftragsprofil` `persoenlich`" in addition, addition)
    require("ohne Internetzugriff" in addition, addition)
    require("Postfach" not in P.INSTRUCTION, "the shared (voice) instruction must not carry the chat rule")


class FailingGmail(RecordingGmail):
    async def message(self, message_id):
        self.calls.append(("message", message_id))
        raise ConnectionError("gmail unreachable")


async def t_a_missing_mail_is_a_completed_refusal_and_the_session_goes_on():
    """Review S1-1: a handler refusal used to become `unknown`, blocking the session."""
    w = mail_world([mail("m1", "Rechnung", "Betrag 120 Euro")], names=("gmail_search", "gmail_read_message"))
    try:
        async def native():
            missing = await w.tools.call(dict(SEARCH, tool="gmail_read_message", callId="c-missing",
                                              arguments={"message_id": "nope"}))
            require(missing["success"] is False, missing)
            require_equal((C.payload(missing)["state"], C.payload(missing)["reason"]), ("failed", "message_not_found"))
            rows = {r["call_id"]: r["state"] for r in C.tool_rows(w)}
            require_equal(rows["c-missing"], "completed", "a refusal is a completed call, not an unknown one")
            after = await w.tools.call(SEARCH)
            require(after["success"], after)
        await C.dispatch(w, native)
    finally:
        close(w)
    world = C.tools_world(names=("gmail_read_message",), register=False)
    w = world.__enter__()
    try:
        w.provider = FailingGmail([])
        G.register(w.router, G.GmailCapabilities(w.provider))

        async def native_down():
            down = await w.tools.call(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": "m1"}))
            require_equal((C.payload(down)["state"], C.payload(down)["reason"]), ("failed", "read_service_unavailable"))
            require_equal([r["state"] for r in C.tool_rows(w)], ["completed"])
        await C.dispatch(w, native_down)
    finally:
        world.__exit__(None, None, None)


async def t_ordinary_newsletters_and_password_prompts_are_delivered_not_failed():
    """Review S1-2: the reply fence read an escaped line break as a credential value."""
    # The first review round measured these as failed calls. The line after a credential
    # word may now be blacked out (what the old fence refused as a whole); the call is
    # delivered and everything around it stays readable (Review S1R3-1 trade-off).
    # Up to LABEL_WINDOW lines after such a word may go (the old fence refused the whole
    # mail); what lies beyond the window stays.
    tail = "\nzwei\ndrei\nvier\nfuenf\nsechs\nImpressum"
    bodies = ["Wir verwenden Cookies\nMehr erfahren" + tail, "Ihre Zugangsdaten:\nBenutzername max" + tail,
              "Passwort:\nBitte vergeben Sie ein neues" + tail, "Ihren Token\nerneuert am Montag" + tail]
    readable = ["Wir verwenden Cookies", "Ihre Zugangsdaten:", "Passwort:", "Ihren Token"]
    messages = [mail("m%d" % i, "Hinweis %d" % i, body) for i, body in enumerate(bodies)]
    w = mail_world(messages, names=("gmail_read_message",))
    try:
        async def native():
            for index in range(len(bodies)):
                reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", callId="c%d" % index,
                                                arguments={"message_id": "m%d" % index}))
                require(reply["success"], reply)
                body = C.payload(reply)["data"]["body"]
                require(readable[index] in body and "Impressum" in body, body)
            require_equal({r["state"] for r in C.tool_rows(w)}, {"completed"})
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_codes_reset_links_and_password_statements_are_blacked_out():
    """Review S1-3: one-time codes and login links reached the model and the evidence."""
    secrets = {"Ihr Bestaetigungscode lautet 482913": "482913", "Anmeldecode: 739104": "739104",
               "Einmalcode 551026": "551026",
               "https://login.example.invalid/magic/aB3dE5gH7jK9mN1pQ3": "aB3dE5gH7jK9mN1pQ3",
               "https://shop.example.invalid/reset?code=Zz99Yy88": "Zz99Yy88",
               "Ihr Passwort: Sommerwind": "Sommerwind", "Das Passwort lautet: Xy7!abcd": "Xy7!abcd",
               "Passwort:\tgeheimwort": "geheimwort",
               # Review S1R2-1: the value on the NEXT line (the usual OTP mail form).
               "Kennwort\nXy7!abcq": "Xy7!abcq", "Ihre neue PIN\n4711": "4711",
               "Ihr API-Token\nabcd1234efgh": "abcd1234efgh", "Ihr Bestaetigungscode:\n\n482917": "482917",
               # Review S1R2-4: the code before its word, sign-in sentences, short links.
               "482918 ist Ihr Bestaetigungscode": "482918", "Verwenden Sie 482919, um sich anzumelden": "482919",
               "G-482920 ist dein Google-Bestaetigungscode": "482920",
               "https://t.example.invalid/Ab3dE5gH7j": "Ab3dE5gH7j", "Zoom Kenncode 482921": "482921",
               # Letters AND digits: only the code-word rule sees these (not the number rule).
               "Ihr Sicherheitscode lautet K7P2QX": "K7P2QX",
               # Review S1R3-1: compounds and passphrases on the next line (the old fence refused these).
               "Ihr Einmalpasswort:\n482922": "482922", "Ihr Initialpasswort:\nXy7!abcr": "Xy7!abcr",
               "Dein Startkennwort:\n\nSommer2024y": "Sommer2024y", "Ihr Passwort:\nblaue Katze tanzt": "blaue Katze tanzt",
               # Review S1R3-2: code phrases ending in a colon, grouped digits, U+2028.
               "Geben Sie den folgenden Code ein:\n\n482923": "482923", "Your verification code is:\n\n482924": "482924",
               "Dein Code lautet:\n482925": "482925", "Verwende diesen Code, um dich anzumelden:\n482926": "482926",
               "482 927 ist dein Instagram-Code": "482 927", "Kennwort\u2028Xy7!abcs": "Xy7!abcs",
               # Only the label rule sees these (no colon, not in the fence's vocabulary).
               "Ihr Zugangscode\n482930": "482930", "Ihre TAN\n731990": "731990", "Dein Passcode\nHx8!kk2": "Hx8!kk2"}
    body = "Guten Tag,\n" + "\n".join(secrets) + "\nFreundliche Gruesse"
    w = mail_world([mail("m1", "Sicherheit", body)], names=("gmail_read_message",))
    try:
        async def native():
            reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": "m1"}))
            require(reply["success"], reply)
            raw = reply["contentItems"][0]["text"]
            for line, value in secrets.items():
                require(value not in raw, "leaked: " + line)
            require("Freundliche Gruesse" in raw and "login.example.invalid" in raw, raw)
            from solvio.agent_runtime import native_observations as O
            preview = json.dumps([O._read_projection(reply, p) for p in O.READ_PREVIEWS])
            for line, value in secrets.items():
                require(value not in preview, "leaked into the evidence preview: " + line)
        await C.dispatch(w, native)
    finally:
        close(w)


async def t_dense_or_wide_threads_fit_characters_and_wire_bytes():
    """Review S1-5: the material fitted single-encoded characters; the reply is
    double-encoded and the wire counts UTF-8 bytes."""
    dense = "Zeile\n\n\"zitiert\" \\ Pfad\n" * 1500
    wide = "\U0001f600\u4e2d\u6587 " * 3000
    # Only the reply-level fit catches these: the material passes its own count,
    # the double-encoded quotes and the 4-byte emoji do not (Review S1-5).
    quotes = '"' * 7000
    emoji = "\U0001f600" * 7000
    for body, count in ((dense, 5), (wide, 5), (quotes, 2), (emoji, 4)):
        messages = [EmailMessage("m%d" % i, "t-x", sender="a@example.invalid", to=OWN, subject="S%d" % i,
                                 body=body, labels=("INBOX",)) for i in range(count)]
        w = mail_world(messages, names=("gmail_read_thread",))
        try:
            async def native():
                reply = await w.tools.call(dict(SEARCH, tool="gmail_read_thread", arguments={"thread_id": "t-x"}))
                require(reply["success"], reply)
                raw = T._json(reply)
                require(len(raw) <= T.MAX_RESPONSE_CHARS and len(raw.encode("utf-8")) <= T.WIRE_REPLY_BYTES,
                        (len(raw), len(raw.encode("utf-8"))))
                require_equal({r["state"] for r in C.tool_rows(w)}, {"completed"})
            await C.dispatch(w, native)
        finally:
            close(w)


async def t_expired_google_price_evidence_stops_reads_before_the_provider():
    """Review S1-6: the reads use the ONE checked Google price observation and its expiry."""
    from datetime import date, timedelta
    from unittest.mock import patch
    from solvio.capabilities import task_action as TA
    w = mail_world([mail("m1", "Rechnung", "x")], names=("gmail_search",))
    try:
        async def native():
            with patch.object(TA, "_today", return_value=TA.GOOGLE_POLICY_EXPIRES + timedelta(days=1)):
                reply = await w.tools.call(SEARCH)
            require(reply["success"] is False, reply)
            require_equal(w.provider.calls, [], "no Google call on expired price evidence")
        await C.dispatch(w, native)
    finally:
        close(w)
    # Both places hold on their own: the price quote AND the execution itself.
    from solvio.agent_runtime import cost_dispatch as CD
    capabilities = G.GmailCapabilities(RecordingGmail([]))
    service = G.GmailTaskRead("gmail_search", capabilities.search)
    arguments = {"query": "x"}
    invocation = CD.ServiceInvocation.bind(capability="gmail_search", version=1, service="google.gmail",
        operation="read", arguments=arguments, resources=service.resources(G.SPECS["gmail_search"], arguments))
    later = date(2099, 1, 1)
    with patch.object(TA, "_today", return_value=later):
        require(service.quote("google.gmail", invocation).upper_bound_cents is None,
                "an expired observation is no price")
        outcome = await service.execute(arguments, None)
    require_equal((outcome.state, outcome.reason), ("not_dispatched", "read_cost_evidence_expired"))
    require_equal(capabilities.provider.calls, [])


async def t_a_search_naming_credentials_is_refused_with_its_own_reason():
    """Review S1-7: it used to surface as `native_tool_authority_ended`."""
    w = mail_world([mail("m1", "Stadtwerke", "x")], names=("gmail_search",))
    try:
        async def native():
            reply = await w.tools.call(dict(SEARCH, arguments={"query": "Zugangsdaten Stadtwerke"}))
            require_equal(C.payload(reply), {"error": "native_tool_argument_names_credentials"})
            require_equal((w.provider.calls, C.tool_rows(w)), ([], []))
        await C.dispatch(w, native)
    finally:
        close(w)


def t_only_a_private_chat_task_gets_mail_and_its_session_has_no_web():
    """Review S1-4 / ADR-0040: private data and a channel to the outside never share a session."""
    from solvio.agent_runtime import task_start_service as TSS
    from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
    from solvio.specialists import native_task_profile as NTP
    require_equal(TR.PRIVATE_DATA_TOOLS, NTP.PRIVATE_DATA_TOOLS)
    require_equal(TR.PRIVATE_DATA_TOOLS, TSS.PRIVATE_DATA_CAPABILITIES)
    require_equal(NTP.web_search_mode({"portal_list", "result_files_list"}), "live")
    require_equal(NTP.web_search_mode({"portal_list", "gmail_search"}), "disabled")
    require_equal(NTP.web_search_mode({"calendar_list_events"}), "disabled")
    receipt = VerifiedTaskReceipt("app_session", "app:chat:n-1", "local-owner")
    arguments = {"objective": "Fasse meine wichtigen Mails der Woche zusammen."}
    plain = TSS.AuthorizedTaskStart.bind(receipt=receipt, request_id="req-private-1", capability="agent_task_task",
                                         arguments=arguments, conversation_ref="c-0123456789abcdef")
    private = TSS.AuthorizedTaskStart.bind(receipt=receipt, request_id="req-private-1", capability="agent_task_task",
                                           arguments=arguments, conversation_ref="c-0123456789abcdef",
                                           private_data=True)
    require(private.arguments_digest != plain.arguments_digest, "the marker is bound in the start digest")
    require(private.matches("agent_task_task", arguments, "local-owner", "trusted_interactive_app"))
    require(not TSS.AuthorizedTaskStart(**{**private.__dict__, "private_data": False}).matches(
        "agent_task_task", arguments, "local-owner", "trusted_interactive_app"), "the marker cannot be dropped")
    require(not TSS.AuthorizedTaskStart(**{**plain.__dict__, "private_data": True}).matches(
        "agent_task_task", arguments, "local-owner", "trusted_interactive_app"), "the marker cannot be added")
    for changes in ({"capability": "agent_task_research"}, {"conversation_ref": ""}):
        try:
            TSS.AuthorizedTaskStart.bind(**{"receipt": receipt, "request_id": "req-private-2",
                "capability": "agent_task_task", "arguments": arguments,
                "conversation_ref": "c-0123456789abcdef", "private_data": True, **changes})
        except ValueError:
            pass
        else:
            raise AssertionError("private data outside a chat task: " + repr(changes))
    w = mail_world([mail("m1", "x", "y")])
    try:
        router = w.router
        starts = TSS.TaskStartService.__new__(TSS.TaskStartService)
        starts.router = router
        names = {entry.name for entry in starts.capabilities_for("task")}
        require(not names & TR.PRIVATE_DATA_TOOLS, names)
        names = {entry.name for entry in starts.capabilities_for("task", private_data=True)}
        registered_mail = set(G.GmailTaskRead.METHODS)   # this world registers mail, not calendar
        require(registered_mail <= names, names)
        names = {entry.name for entry in starts.capabilities_for("research", private_data=True)}
        require(not names & TR.PRIVATE_DATA_TOOLS, "only the general task scope carries private data")
    finally:
        close(w)


def t_everything_the_first_version_refused_is_now_blacked_out_never_delivered():
    """Review S1R4-1 (criterion 1): the first version (1842683) refused a reply whose
    JSON-encoded text formed a credential line. Every such mail must now arrive with
    its value blacked out — across up to two lines between label and value."""
    # Review S1R5-1: labels outside any word list (the fence decides) and up to three
    # lines between label and value.
    labels = ["Ihr Passwort", "Ihr neues Passwort", "Kennwort", "Ihre PIN", "Ihr Einmalpasswort:",
              "Ihr API-Token", "Zugangsdaten", "Ihr Initialkennwort", "Secret", "Geheimnis", "Passphrase",
              "Credentials", "Auth", "Session-ID", "Cookie"]
    middles = [[], ["für das Kundenkonto"], ["für die Karte:", "bitte gut merken"],
               ["(bitte notieren)", "wie besprochen", "und nicht weitergeben"]]
    verbs = ["", "lautet ", "ist ", ": "]
    values = ["Xy7!abcd", "4711", "Sommer2024x", "abcd1234efgh", "blaue Katze tanzt"]
    refused_before, leaked = 0, []
    for label in labels:
        for middle in middles:
            for verb in verbs:
                for value in values:
                    body = "\n".join([label, *middle, verb + value])
                    if TR._passes(json.dumps(body, ensure_ascii=False)):
                        continue                      # the first version delivered it as well
                    refused_before += 1
                    if value in "\n".join(line if isinstance(line, str) else "\n".join(line)
                                          for line in [TR.task_material({"body": body})["body"]]):
                        leaked.append(body)
    require(refused_before > 900, refused_before)
    require_equal(leaked[:5], [], "delivered in clear although the first version refused it")


def t_booking_numbers_postcodes_years_and_readable_links_stay():
    """Review S1R3-3: the code rules must not eat the content an errand needs."""
    kept = ["Ihre Buchungsbestaetigung Nr. 58392017 fuer das Hotel Alster", "Bestellbestätigung 40291833",
            "Rechnung 20261234 bitte bestätigen", "Anmeldung: Musterstr. 1, 10115 Berlin",
            "Anmeldeschluss ist Ende 2026, bitte bestätigen", "Bitte den Code\nreviewen",
            "https://hotel.example.invalid/hotel-alster-2026/zimmer",
            # A sign-in word AND a receipt number in one line: the number stays.
            "Zum Anmelden die Buchungsnummer 58392017 bereithalten"]
    for text in kept:
        require_equal(TR._scrub(text), text)


class SlowGmail(RecordingGmail):
    async def search(self, query="", *, limit=10, label=""):
        self.calls.append(("search", query, limit, label))
        await asyncio.sleep(2)
        return []


async def t_a_slow_read_ends_completed_before_the_router_deadline():
    """Review S1R2-2: the router's deadline turned a slow read into `unknown`."""
    from unittest.mock import patch
    world = C.tools_world(names=("gmail_search",), register=False)
    w = world.__enter__()
    try:
        w.provider = SlowGmail([])
        G.register(w.router, G.GmailCapabilities(w.provider))

        async def native():
            with patch.object(TR, "READ_DEADLINE_MARGIN", G.SPECS["gmail_search"].timeout - 0.3):
                reply = await w.tools.call(SEARCH)
            require_equal((C.payload(reply)["state"], C.payload(reply)["reason"]), ("failed", "read_service_unavailable"))
            require_equal([r["state"] for r in C.tool_rows(w)], ["completed"])
        await C.dispatch(w, native)
    finally:
        world.__exit__(None, None, None)


async def t_a_slow_material_is_withheld_before_the_router_deadline():
    """Review S1R5-2: preparing a long thread ran after the read deadline and could
    reach the router's deadline. It has its own budget and withholds instead."""
    from unittest.mock import patch
    w = mail_world([mail("m1", "Rechnung", "Betrag 120 Euro")], names=("gmail_read_message",))
    try:
        async def native():
            with patch.object(TR, "MATERIAL_DEADLINE_MARGIN", G.SPECS["gmail_read_message"].timeout + 1):
                reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": "m1"}))
            require(reply["success"], reply)
            require_equal(C.payload(reply)["data"]["withheld"], "result_too_large")
            require_equal([r["state"] for r in C.tool_rows(w)], ["completed"])
        await C.dispatch(w, native)
    finally:
        close(w)


def t_a_thread_that_cannot_fit_is_withheld_without_scrubbing_it_first():
    """Review S1R6-2: long threads that are withheld anyway were scrubbed completely."""
    from unittest.mock import patch
    calls = []
    thread = {"messages": [{"body": "x" * 8000, "subject": "S"} for _ in range(60)]}
    with patch.object(TR, "_scrub", side_effect=lambda text: calls.append(1) or text):
        require_equal(TR.task_material(thread)["withheld"], "result_too_large")
    require_equal(calls, [], "nothing was scrubbed for a result that cannot fit")


async def t_preparing_mail_does_not_stall_the_core_event_loop():
    """Review S1R6-2: the preparation ran on the event loop and stalled voice and chat."""
    import time as clock
    from unittest.mock import patch
    w = mail_world([mail("m1", "Rechnung", "Betrag 120 Euro")], names=("gmail_read_message",))
    original = TR._scrub
    try:
        async def native():
            gaps, stop = [], asyncio.Event()

            async def ticker():
                last = clock.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.02)
                    now = clock.monotonic()
                    gaps.append(now - last)
                    last = now
            task = asyncio.ensure_future(ticker())
            with patch.object(TR, "_scrub", side_effect=lambda text: clock.sleep(0.4) or original(text)):
                reply = await w.tools.call(dict(SEARCH, tool="gmail_read_message", arguments={"message_id": "m1"}))
            stop.set()
            await task
            require(reply["success"], reply)
            require(max(gaps) < 0.3, "the event loop stalled for %.2f s" % max(gaps))
        await C.dispatch(w, native)
    finally:
        close(w)


def t_a_granted_private_tool_that_is_not_registered_stops_the_manifest():
    """Review S1R2-3: a missing registration must not turn web search back on
    for a session whose history may hold mail."""
    world = C.tools_world(names=("gmail_search",), register=False)
    w = world.__enter__()
    try:
        try:
            w.tools.manifest()
        except ValueError as exc:
            require_equal(str(exc), "native_tool_private_tools_unavailable")
        else:
            raise AssertionError("manifest without the granted private tool was served")
    finally:
        world.__exit__(None, None, None)


def t_a_revoked_private_grant_stops_the_manifest_instead_of_opening_the_web():
    """Review S1R3, note b: revoked between checks, the manifest came back empty and
    the worker would have started with web search."""
    w = mail_world([mail("m1", "x", "y")], names=("gmail_search",))
    try:
        require_equal([tool["name"] for tool in w.tools.manifest()], ["gmail_search"])
        require(w.authority.revoke(w.grant.reference, "test:owner-revoked"))
        try:
            w.tools.manifest()
        except ValueError as exc:
            require_equal(str(exc), "native_tool_private_tools_unavailable")
        else:
            raise AssertionError("a revoked private grant yielded a (web) manifest")
    finally:
        close(w)


def t_an_order_with_private_tools_is_not_offered_to_the_claude_worker():
    """Review S1R2-5: the Claude worker may not call the mail tools."""
    from solvio.agent_runtime import provider_switch as PS
    w = mail_world([mail("m1", "x", "y")], names=("gmail_search",))
    try:
        with w.ledger._open() as db:
            require(PS._claude_worker_compatible(db, w.ledger.get_run(w.run), w.ledger.get_task(w.task), [],
                                                 ledger=w.ledger) is False)
    finally:
        close(w)


def t_the_projection_of_mail_material_keeps_digest_size_and_a_marked_preview():
    from solvio.agent_runtime import native_observations as O
    text = json.dumps({"state": "succeeded", "data": {"body": "x" * 5000}})
    response = {"success": True, "contentItems": [{"type": "inputText", "text": text}]}
    import hashlib
    for preview in O.READ_PREVIEWS:
        projection = O._read_projection(response, preview)
        require_equal(projection["payload_sha256"], hashlib.sha256(text.encode()).hexdigest())
        require_equal(projection["payload_chars"], len(text))
        if preview:
            require_equal(projection["preview"], text[:preview])
            require(projection["complete"] is False)
        else:
            require("preview" not in projection)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
