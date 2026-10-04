"""Real native service clients, a temporary canonical Vault, transport fixtures.

No Google, Home Assistant or model endpoint is contacted. Public task-entry
tests can reuse native_fixture without manufacturing an internal task.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
from email import message_from_bytes, policy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.agent_runtime import action_contract as AC, store as S, costs as C, cost_dispatch as CD
from solvio.agent_runtime.task_authority import TaskAuthority, VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import TaskStepAuthority
from solvio.capabilities import task_action as TA, policy as AP
from solvio.capabilities.home_assistant import HAExposure
from solvio.integrations.google_calendar import GoogleCalendar
from solvio.integrations.gmail import Gmail
from solvio.integrations.home_assistant import HomeAssistant
from solvio.secret_vault import admin, keyring as K, policy as VP, context as SC
from solvio.secret_vault.broker import SecretBroker
from solvio.secret_vault.store import VaultStore


def _gmail_message(mid, to, subject, body, *, thread="thread1", labels=("DRAFT",)):
    return {"id": mid, "threadId": thread, "labelIds": list(labels),
        "payload": {"mimeType": "text/plain", "headers": [{"name": "To", "value": to},
            {"name": "From", "value": "fixture@example.invalid"},
            {"name": "Subject", "value": subject}],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}


class Transport:
    def __init__(self):
        self.events, self.drafts, self.messages = {}, {}, {}
        self.states = {"light.fixture": {"entity_id": "light.fixture", "state": "off",
            "attributes": {"friendly_name": "Fixture light", "brightness": 0}}}
        self.exposed = {"light.fixture": {"conversation": True}}
        self.calls, self.mutations, self.contexts = [], [], []
        self.drop_after_write = False
        self.tamper_after_write = False
        self.before_response = None
        self.auth_error = ""

    def dispatch(self, method, url, params, body):
        self.calls.append((method, url))
        self.contexts.append((SC.current().origin, SC.current().capability))
        if url == "https://oauth2.googleapis.com/token":
            return (400, {"error": self.auth_error}) if self.auth_error else (
                200, {"access_token": "synthetic-native-refreshed", "expires_in": 3600})
        status, reply = 200, {}
        if "/calendar/v3/" in url:
            suffix = url.split("/events", 1)[1]
            eid = suffix.lstrip("/")
            if method == "GET" and not eid:
                reply = {"items": list(self.events.values())}
            elif method == "GET":
                status, reply = (200, self.events[eid]) if eid in self.events else (404, {})
            elif method == "POST":
                eid = body["id"]
                if eid in self.events:
                    return 409, {}
                self.events[eid] = deepcopy(body)
                if self.tamper_after_write:
                    self.events[eid]["summary"] = "Unexpected native contents"
                reply = self.events[eid]
            elif method == "PATCH":
                self.events[eid].update(deepcopy(body))
                reply = self.events[eid]
            elif method == "DELETE":
                self.events.pop(eid, None)
                status, reply = 204, {}
        elif "/gmail/v1/users/me" in url:
            path = url.split("/users/me", 1)[1]
            if method == "POST" and path == "/drafts":
                mime = message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]), policy=policy.default)
                number = len(self.drafts) + 1
                did, mid = f"draft{number}", f"message{number}"
                message = _gmail_message(mid, mime["To"], mime["Subject"], mime.get_content(),
                    thread=body["message"].get("threadId", "thread1"))
                if mime["In-Reply-To"]:
                    message["payload"]["headers"].append({"name": "In-Reply-To", "value": mime["In-Reply-To"]})
                if self.tamper_after_write:
                    message["payload"]["headers"][2]["value"] = "Unexpected subject"
                self.drafts[did] = {"id": did, "message": message}
                reply = {"id": did, "message": {"id": mid}}
            elif method == "POST" and path == "/drafts/send":
                draft = self.drafts.pop(body["id"])
                message = deepcopy(draft["message"])
                if (body.get("message") or {}).get("raw"):
                    mime = message_from_bytes(base64.urlsafe_b64decode(body["message"]["raw"]), policy=policy.default)
                    message = _gmail_message(message["id"], mime["To"], mime["Subject"], mime.get_content())
                    message["payload"]["headers"][1]["value"] = mime["From"]
                message["labelIds"] = ["SENT"]
                self.messages[message["id"]] = message
                reply = {"id": message["id"]}
            elif method == "GET" and path.startswith("/drafts/"):
                did = path.split("/")[-1]
                status, reply = (200, self.drafts[did]) if did in self.drafts else (404, {})
            elif method == "GET" and path == "/messages":
                reply = {"messages": [{"id": mid} for mid in self.messages]}
            elif method == "GET" and path.startswith("/messages/"):
                mid = path.split("/")[-1]
                status, reply = (200, self.messages[mid]) if mid in self.messages else (404, {})
            elif method == "GET" and path == "/profile":
                reply = {"emailAddress": "fixture@example.invalid"}
            else:
                raise RuntimeError("unhandled fixture Gmail operation")
        elif url.startswith("http://127.0.0.1:8123/api/"):
            path = url.split("/api/", 1)[1]
            if method == "GET" and path == "states":
                reply = list(self.states.values())
            elif method == "GET" and path.startswith("states/"):
                reply = self.states[path.split("/", 1)[1]]
            elif method == "POST" and path.startswith("services/"):
                state = self.states[body["entity_id"]]
                if not self.tamper_after_write:
                    state["state"] = "off" if path.endswith("turn_off") else "on"
                    if "brightness_pct" in body:
                        state["attributes"]["brightness"] = round(body["brightness_pct"] * 255 / 100)
                reply = []
            else:
                raise RuntimeError("unhandled fixture HA operation")
        else:
            raise RuntimeError("fixture rejects all other endpoints")
        if method in {"POST", "PATCH", "DELETE"}:
            self.mutations.append((method, url, deepcopy(body)))
            if self.drop_after_write:
                raise TimeoutError("synthetic lost reply after native effect")
        return status, json.loads(json.dumps(reply))


class Reply:
    def __init__(self, transport, method, url, params=None, json=None, **_):
        self.transport, self.method, self.url = transport, method, url
        self.params, self.body = params, json or {}
        self.status, self.payload = 0, {}
        self.content_length = 1

    async def __aenter__(self):
        self.status, self.payload = self.transport.dispatch(self.method, self.url, self.params, self.body)
        if self.transport.before_response:
            await self.transport.before_response(self.method, self.url)
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, **_):
        return self.payload

    async def text(self):
        return json.dumps(self.payload)

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("fixture http error")


class Websocket:
    def __init__(self, transport):
        self.transport, self.command = transport, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def send_json(self, message):
        self.command = message

    async def receive_json(self):
        if self.command is None:
            return {"type": "auth_required"}
        if self.command["type"] == "auth":
            return {"type": "auth_ok"}
        kind = self.command["type"]
        value = {"exposed_entities": deepcopy(self.transport.exposed)} if kind == "homeassistant/expose_entity/list" else []
        return {"success": True, "result": value}


class Session:
    def __init__(self, transport, **_):
        self.transport = transport

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def request(self, method, url, **kwargs):
        return Reply(self.transport, method, url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def ws_connect(self, *_args, **_kwargs):
        return Websocket(self.transport)


@contextmanager
def native_fixture(directory=None):
    with tempfile.TemporaryDirectory(prefix="solvio-action-native-", dir=directory) as folder:
        root = Path(folder)
        with patch.dict(os.environ, {"SOLVIO_VAULT_DIR": str(root / "vault"),
                "SOLVIO_VAULT_TEST_KEYSTORE": str(root / "keys"), "SOLVIO_STATE_DIR": str(root / "state")}):
            K.forget_kek()
            store = VaultStore()
            admin.initialize(store)
            google_caps = tuple(name for (service, _), name in TA._SECRET_CAPABILITIES.items()
                                if service in {"calendar", "gmail"})
            for ref in (GoogleCalendar.CLIENT_SECRET_REF, GoogleCalendar.REFRESH_TOKEN_REF):
                admin.add(secret_ref=ref, kind=VP.SecretKind.PASSWORD, plaintext=b"synthetic-native-fixture-value",
                    allowed_capabilities=google_caps, allowed_targets=("https://oauth2.googleapis.com",),
                    allowed_executors=(VP.ExecutorId.HTTP,), allow_background=True, store=store)
            admin.add(secret_ref="secret://ha/token", kind=VP.SecretKind.PASSWORD,
                plaintext=b"synthetic-ha-fixture-value", allowed_capabilities=("ha_turn_on", "ha_turn_off", "ha_set_brightness"),
                allowed_targets=("http://127.0.0.1:8123",), allowed_executors=(VP.ExecutorId.HOME_ASSISTANT,),
                allow_background=True, store=store)
            broker = SecretBroker(store)
            calendar = GoogleCalendar(client_id="fixture.apps.googleusercontent.com", calendar_id="primary", broker=broker)
            gmail = Gmail(client_id="fixture.apps.googleusercontent.com", broker=broker)
            for client in (calendar, gmail):
                client._access_token = "synthetic-native-access"
                client._expires_at = time.monotonic() + 3600
                client._token_credential_versions = client._credential_versions()
            ha = HomeAssistant("http://127.0.0.1:8123", broker=broker, credential_ref="secret://ha/token")
            transport = Transport()
            with patch("aiohttp.ClientSession", lambda **kwargs: Session(transport, **kwargs)), \
                    patch.object(TA, "_today", lambda: TA.GOOGLE_POLICY_CHECKED):
                yield SimpleNamespace(root=root, calendar=calendar, gmail=gmail, ha=ha,
                    exposure=HAExposure(ha, ttl=0), broker=broker, transport=transport)
            K.forget_kek()


def calendar_action(client, operation="create", *, action_id="a1", event_id="event1"):
    target = {"calendar_id": client.calendar_id}
    if operation in {"update", "delete"}:
        target["event_id"] = event_id
    payload = {"start": "2026-09-15T10:00:00+02:00", "end": "2026-09-15T11:00:00+02:00"}
    if operation in {"create", "update"}:
        payload.update(summary="SYNTHETIC calendar appointment", all_day=False, description="Fixture text", location="Test room")
    elif operation == "delete":
        payload = {}
    return {"action_id": action_id, "service": "calendar", "operation": operation,
            "account": TA.account_identity("calendar", client), "target": target, "payload": payload}


def gmail_action(client, operation="create_draft", *, action_id="a1", draft_id="draft1"):
    target = {"mailbox": "me"}
    payload = {"subject": "SYNTHETIC message", "body": "Fixture body"}
    if operation == "create_draft":
        target.update(to="recipient@example.invalid", reply_to_message="")
        payload.update(thread_id="", in_reply_to="")
    elif operation == "send_draft":
        target["draft_id"] = draft_id
        payload["to"] = "recipient@example.invalid"
    else:
        payload = {"query": "subject:SYNTHETIC", "limit": 5}
    return {"action_id": action_id, "service": "gmail", "operation": operation,
            "account": TA.account_identity("gmail", client), "target": target, "payload": payload}


def ha_action(client, *, brightness=None):
    return {"action_id": "a1", "service": "ha", "account": TA.account_identity("ha", client),
        "operation": "set_state" if brightness is None else "set_brightness",
        "target": {"entity_id": "light.fixture"},
        "payload": {"state": "on"} if brightness is None else {"brightness_pct": brightness}}


@contextmanager
def action_fixture(native, action):
    ledger = S.AgentRunLedger(str(native.root / "agent.sqlite3"))
    AC.initialize(ledger)
    task = ledger.create_task(objective="Perform exactly the submitted synthetic service action",
        scope="research", created_origin="trusted_interactive_app", created_principal="owner:fixture")
    run = ledger.create_run(task_id=task.task_id)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    C.CostLedger(ledger).configure(task.task_id)
    prepared = AC.prepare(AC.from_payload({"actions": [action]}), task_id=task.task_id, run_id=run.run_id)
    with ledger._open() as connection:
        AC.record_prepared(connection, prepared, now=time.time())
    authority = TaskAuthority(ledger)
    grant = authority.issue(task.task_id, run.run_id,
        receipt=VerifiedTaskReceipt("app_session", "fixture:owner-command", "owner:fixture"),
        capabilities=(prepared.capability_grant,), expires_at=time.time() + 3600)
    bound = AC.for_run(ledger, run.run_id)
    arguments = bound.action_arguments(action["action_id"])
    step = ledger.create_step(run_id=run.run_id, seq=1, kind="capability", capability=TA.SPEC.name)
    ledger.update_step(step.step_id, state="running")
    claim = authority.claim_step(grant.reference, step.step_id, TA.SPEC.name, arguments, 1,
        task_id=task.task_id, run_id=run.run_id)
    require(claim.allowed)
    adapter = TA.TaskServiceAction(ledger, calendar=native.calendar, gmail=native.gmail,
                                  ha=native.ha, exposure=native.exposure)
    binding = TaskStepAuthority(grant.reference, task.task_id, run.run_id, step.step_id)
    with SC.bound(SC.UseContext(origin=AP.OriginClass.BACKGROUND_AUTOMATION,
            principal="owner:fixture", capability=TA.SPEC.name)):
        yield SimpleNamespace(ledger=ledger, task=task.task_id, run=run.run_id, step=step.step_id,
            bound=bound, grant=grant, authority=authority, arguments=arguments, adapter=adapter, binding=binding)


async def t_calendar_create_uses_native_readback_and_durable_receipt():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require(outcome.ok)
        require_equal(len(native.transport.mutations), 1)
        receipts = AC.read_receipts(w.ledger, w.run)
        require_equal(len(receipts), 1)
        require(receipts[0]["native"]["observed"]["confirmed"])
        require_equal(native.transport.calls[-1][0], "GET")
        require(all(item == (AP.OriginClass.BACKGROUND_AUTOMATION, "calendar_create_event")
                    for item in native.transport.contexts))


async def t_calendar_wrong_native_content_is_not_success():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        native.transport.tamper_after_write = True
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require_equal(outcome.state, "completed")
        require(not outcome.ok)


async def t_calendar_update_and_delete_use_exact_event_id():
    for operation in ("update", "delete"):
        with native_fixture() as native:
            native.transport.events["event1"] = {"id": "event1", "summary": "Old title", "description": "",
                "location": "", "start": {"dateTime": "2026-09-14T09:00:00+02:00"},
                "end": {"dateTime": "2026-09-14T10:00:00+02:00"}}
            with action_fixture(native, calendar_action(native.calendar, operation)) as w:
                outcome = await w.adapter.execute(w.arguments, w.binding)
                require(outcome.ok)
                require(all(url.endswith("/event1") for _, url in native.transport.calls))


async def t_calendar_list_uses_bound_absolute_window():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar, "list")) as w:
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require(outcome.ok)
        require_equal(native.transport.mutations, [])


async def t_calendar_all_day_create_and_update_preserve_calendar_dates():
    for operation in ("create", "update"):
        with native_fixture() as native:
            native.transport.events["event1"] = {"id": "event1", "summary": "Old", "start": {"date": "2026-09-14"}, "end": {"date": "2026-09-15"}}
            action = calendar_action(native.calendar, operation)
            action["payload"].update(all_day=True, start="2026-09-15T00:00:00+00:00", end="2026-09-16T00:00:00+00:00")
            with action_fixture(native, action) as w:
                outcome = await w.adapter.execute(w.arguments, w.binding)
                require(outcome.ok)
                require_equal(len(native.transport.mutations), 1)


async def t_unknown_write_never_executes_the_action_again():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        native.transport.drop_after_write = True
        first = await w.adapter.execute(w.arguments, w.binding)
        require_equal(first.state, "unknown")
        native.transport.drop_after_write = False
        second = await w.adapter.execute(w.arguments, w.binding)
        require_equal(second.reason, "action_already_claimed")
        require_equal(len(native.transport.mutations), 1)


async def t_vault_denial_is_non_dispatch_not_an_unknown_effect():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        with SC.bound(SC.UseContext(origin=AP.OriginClass.UNSPECIFIED)):
            outcome = await w.adapter.execute(w.arguments, w.binding)
        require_equal(outcome.state, "not_dispatched")
        require_equal(outcome.reason, "action_account_access_required")
        require_equal(native.transport.calls, [])


async def t_expired_native_google_login_holds_without_effect():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        native.calendar._access_token = ""
        native.transport.auth_error = "invalid_grant"
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require_equal(outcome.state, "not_dispatched")
        require_equal(outcome.reason, "action_account_access_required")
        require_equal(native.transport.mutations, [])


async def t_revocation_during_refresh_prevents_the_native_write():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        native.calendar._access_token = ""
        async def revoke(method, url):
            w.authority.revoke(w.grant.reference, "fixture:owner-cancel")
        native.transport.before_response = revoke
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require_equal(outcome.state, "not_dispatched")
        require_equal(native.transport.mutations, [])


async def t_instance_helper_swap_during_read_cannot_bypass_prewrite_grant_check():
    with native_fixture() as native:
        native.transport.events["event1"] = {"id": "event1", "summary": "Old", "start": {"dateTime": "2026-09-15T08:00:00+02:00"}, "end": {"dateTime": "2026-09-15T09:00:00+02:00"}}
        with action_fixture(native, calendar_action(native.calendar, "update")) as w:
            async def swap(method, url):
                w.adapter._unchanged = lambda *_: None
            native.transport.before_response = swap
            outcome = await w.adapter.execute(w.arguments, w.binding)
            require_equal(outcome.state, "not_dispatched")
            require_equal(native.transport.mutations, [])


async def t_grant_revocation_during_native_read_prevents_write():
    with native_fixture() as native:
        native.transport.events["event1"] = {"id": "event1", "summary": "Old", "start": {"dateTime": "2026-09-15T08:00:00+02:00"}, "end": {"dateTime": "2026-09-15T09:00:00+02:00"}}
        with action_fixture(native, calendar_action(native.calendar, "update")) as w:
            async def revoke(method, url):
                w.authority.revoke(w.grant.reference, "fixture:owner-cancel")
            native.transport.before_response = revoke
            outcome = await w.adapter.execute(w.arguments, w.binding)
            require_equal(outcome.state, "not_dispatched")
            require_equal(native.transport.mutations, [])


def t_account_rotation_and_replaced_native_method_have_no_price_claim():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        resources = w.adapter.resources(TA.SPEC, w.arguments, w.binding)
        invocation = CD.ServiceInvocation.bind(capability=TA.SPEC.name, version=1,
            service="native.task-action", operation="execute", arguments=w.arguments, resources=resources)
        quote = w.adapter.quote("native.task-action", invocation)
        require_equal(quote.upper_bound_cents, 0)
        require(quote.validate_before_dispatch())
        native.calendar.calendar_id = "other-calendar"
        require(not quote.validate_before_dispatch())
        native.calendar.calendar_id = "primary"
        native.calendar.create_event = lambda **_: None
        require(not quote.validate_before_dispatch())


def t_google_price_observation_expires_instead_of_claiming_free_forever():
    with native_fixture() as native, action_fixture(native, calendar_action(native.calendar)) as w:
        resources = w.adapter.resources(TA.SPEC, w.arguments, w.binding)
        invocation = CD.ServiceInvocation.bind(capability=TA.SPEC.name, version=1,
            service="native.task-action", operation="execute", arguments=w.arguments, resources=resources)
        with patch.object(TA, "_today", lambda: TA.GOOGLE_POLICY_EXPIRES + timedelta(days=1)):
            require_equal(w.adapter.quote("native.task-action", invocation).upper_bound_cents, None)


async def t_gmail_draft_is_read_back_and_never_sent():
    with native_fixture() as native, action_fixture(native, gmail_action(native.gmail)) as w:
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require(outcome.ok)
        require_equal(len(native.transport.drafts), 1)
        require_equal(native.transport.messages, {})
        require(not any(url.endswith("/send") for _, url in native.transport.calls))


async def t_gmail_reply_uses_native_rfc_message_id_not_google_object_id():
    with native_fixture() as native:
        original = _gmail_message("original1", "fixture@example.invalid", "Original", "Incoming fixture")
        original["payload"]["headers"][1]["value"] = "recipient@example.invalid"
        original["payload"]["headers"].append({"name": "Message-ID", "value": "<original@example.invalid>"})
        native.transport.messages["original1"] = original
        action = gmail_action(native.gmail)
        action["target"]["reply_to_message"] = "original1"
        action["payload"].update(thread_id="thread1", in_reply_to="<original@example.invalid>")
        with action_fixture(native, action) as w:
            outcome = await w.adapter.execute(w.arguments, w.binding)
            require(outcome.ok)
            require_equal(len(native.transport.mutations), 1)


async def t_gmail_changed_draft_is_not_confirmed():
    with native_fixture() as native, action_fixture(native, gmail_action(native.gmail)) as w:
        native.transport.tamper_after_write = True
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require_equal(outcome.state, "completed")
        require(not outcome.ok)


async def t_ha_owner_security_override_is_preserved():
    with native_fixture() as native, action_fixture(native, ha_action(native.ha)) as w:
        w.adapter.security_entities = frozenset({"light.fixture"})
        outcome = await w.adapter.execute(w.arguments, w.binding)
        require_equal(outcome.state, "not_dispatched")
        require_equal(native.transport.mutations, [])


def t_gmail_send_from_a_structured_task_is_refused_adr_0041():
    """ADR-0041: jede Mail, die das Haus verlaesst, sieht der Mensch vorher mit Face ID.
    Ein strukturierter Auftrag kann das nicht zusichern (sein Start kann eine
    Dashboard-Sitzung sein); bis die Freigabe je Auftrag den Inhalt bindet, versendet
    er keine Mail. Die frueheren Versandproben dieses Wegs stehen in der Geschichte."""
    with native_fixture() as native:
        native.transport.drafts["draft1"] = {"id": "draft1", "message": _gmail_message(
            "message1", "recipient@example.invalid", "SYNTHETIC message", "Fixture body")}
        try:
            with action_fixture(native, gmail_action(native.gmail, "send_draft")):
                pass
        except ValueError as exc:
            require_equal(str(exc), "action_mail_send_requires_face_id")
        else:
            raise AssertionError("a structured task bound a mail send")
        require_equal(native.transport.mutations, [])


async def t_a_mail_send_bound_before_adr_0041_is_refused_at_execution():
    """Die zweite Sperre, allein geprueft: ein Versandvertrag, der die erste (prepare)
    umgangen haette oder vor ADR-0041 gebunden wurde, versendet trotzdem nichts."""
    with native_fixture() as native:
        native.transport.drafts["draft1"] = {"id": "draft1", "message": _gmail_message(
            "message1", "recipient@example.invalid", "SYNTHETIC message", "Fixture body")}
        with patch.object(AC, "_mail_send_requested", lambda request: False), \
                action_fixture(native, gmail_action(native.gmail, "send_draft")) as w:
            outcome = await w.adapter.execute(w.arguments, w.binding)
        require(not outcome.ok, str(outcome))
        require_equal(outcome.reason, "action_mail_send_requires_face_id", str(outcome))
        require_equal(native.transport.mutations, [])


async def t_gmail_search_returns_native_messages_without_mutation():
    with native_fixture() as native:
        native.transport.messages["message1"] = _gmail_message("message1", "recipient@example.invalid", "SYNTHETIC message", "Fixture body", labels=("SENT",))
        with action_fixture(native, gmail_action(native.gmail, "search")) as w:
            outcome = await w.adapter.execute(w.arguments, w.binding)
            require(outcome.ok)
            require_equal(native.transport.mutations, [])


async def t_ha_state_and_brightness_use_real_client_readback():
    for brightness in (None, 50):
        with native_fixture() as native, action_fixture(native, ha_action(native.ha, brightness=brightness)) as w:
            outcome = await w.adapter.execute(w.arguments, w.binding)
            require(outcome.ok)
            require_equal(len(native.transport.mutations), 1)


async def t_ha_no_effect_and_unexposed_target_never_claim_success():
    for exposed in (True, False):
        with native_fixture() as native, action_fixture(native, ha_action(native.ha, brightness=50)) as w:
            if exposed:
                native.transport.tamper_after_write = True
            else:
                native.transport.exposed.clear()
            outcome = await w.adapter.execute(w.arguments, w.binding)
            require(not outcome.ok)
            require_equal(len(native.transport.mutations), 1 if exposed else 0)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
