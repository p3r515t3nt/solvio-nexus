"""Execute one immutable task action through existing native service clients.

The public model names a bound action; it does not supply a URL, recipient,
relative date, credential or provider callback. Native reads verify the actual
effect. A lost response consumes the action claim and can never cause a retry.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import date, datetime, timezone
from email.utils import getaddresses
import hashlib
import ipaddress
import json
import os
import re
import stat
from types import MethodType
from urllib.parse import urlsplit

from solvio.capabilities.contract import CapabilityDeclined, CapabilityRefused, CapabilitySpec, ExecutionClass, ExecutorUnavailable
from solvio.integrations import gmail as GM, google_calendar as GC, home_assistant as HA
from solvio.capabilities.calendar import CalendarAuthError
from solvio.capabilities.gmail import _message_of, _normalized, extract_address
from solvio.capabilities.home_assistant import HAExposure
from solvio.capabilities.portal import PortalCapabilities
from solvio.portal import readout as PR
from solvio.portal.binding import BINDINGS, PortalBinding
from solvio.portal.client import PortalClient, PortalUnavailable, WorkerBuildMismatch
from solvio.secret_vault.broker import SecretBroker, SecretDenied, SecretUnavailable
from solvio.secret_vault.store import VaultStore
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE
from solvio.tools.base import RiskLevel

SPEC = CapabilitySpec(name="task_service_action", version=1,
    execution_class=ExecutionClass.CONTROLLED, base_risk=RiskLevel.MUTATING,
    semantics=NON_IDEMPOTENT_WRITE, timeout=60.0, cancellable=True,
    input_schema={"type": "object", "additionalProperties": False,
        "required": ["resource_id", "contract_digest", "action_id"],
        "properties": {name: {"type": "string"} for name in
                       ("resource_id", "contract_digest", "action_id")}},
    description="Fuehrt genau eine bereits gebundene Diensthandlung dieses Auftrags aus und liest ihr Ergebnis nach.")

# Primary sources read 2026-09-11 and again 2026-09-25 (DEBT-0325): "All standard
# use of the Calendar/Gmail API is available at no additional cost." Over-quota
# billing is planned "later in 2026 with at least 90 days' notice"; no notice was
# given as of 2026-09-25 (pages last updated 2026-09-10/11).
# https://developers.google.com/workspace/calendar/api/guides/quota?hl=en
# https://developers.google.com/workspace/gmail/api/reference/quota?hl=en
# This observation expires; a subscription/login is never a pricing proof.
GOOGLE_POLICY_CHECKED = date(2026, 9, 25)
GOOGLE_POLICY_EXPIRES = date(2026, 10, 25)
GOOGLE_POLICY_REFERENCE = "google:standard-rest-no-extra:2026-09-25:expires-2026-10-25"

_NATIVE = {
    "calendar": (GC.GoogleCalendar, ("list_events", "get_event", "create_event", "update_event", "delete_event", "_request", "_token", "_credentials", "_authorise_cached", "_credential_versions", "_exchange")),
    "gmail": (GM.Gmail, ("profile", "search", "message", "get_draft", "create_draft", "send_draft", "_request", "_token", "_credentials", "_authorise_cached", "_credential_versions")),
    "ha": (HA.HomeAssistant, ("state", "states", "call_service", "_get", "_auth", "_headers_with", "ws_commands")),
}
_NATIVE_METHODS = {service: {name: getattr(kind, name) for name in names}
                   for service, (kind, names) in _NATIVE.items()}
_BROKER_DESCRIBE = SecretBroker.describe
_STORE_ROW = VaultStore.row
_NATIVE_MIME = GM.Gmail._mime
_EXPOSURE_METHODS = {name: getattr(HAExposure, name) for name in
                     ("boundary", "_load", "is_exposed", "live_state")}
_PORTAL_METHODS = {name: getattr(PortalCapabilities, name) for name in ("status",)}
_PORTAL_REDUCERS = dict(PR.REDUCERS)
_PORTAL_CLIENT_METHODS = {name: getattr(PortalClient, name)
    for name in ("available", "call", "ping", "verify_build", "read", "list_sessions")}
_SECRET_CAPABILITIES = {
    ("calendar", "create"): "calendar_create_event", ("calendar", "update"): "calendar_update_event",
    ("calendar", "delete"): "calendar_delete_event", ("calendar", "list"): "calendar_list_events",
    ("gmail", "create_draft"): "gmail_create_draft", ("gmail", "compose_draft"): "gmail_create_draft", ("gmail", "send_draft"): "gmail_send_draft",
    ("gmail", "search"): "gmail_search", ("ha", "set_brightness"): "ha_set_brightness",
    ("portal", "status"): "portal_status",
    ("portal", "connect"): "portal_login",
}


def _today():
    return datetime.now(timezone.utc).date()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _original(instance, kind, methods):
    if type(instance) is not kind:
        return False
    return all(type(getattr(instance, name, None)) is MethodType
               and getattr(instance, name).__self__ is instance
               and getattr(instance, name).__func__ is implementation
               for name, implementation in methods.items())


def _credentials(client, service):
    """Only safe, versioned vault metadata; never open or hash credential values."""
    broker = getattr(client, "_broker", None)
    if (type(broker) is not SecretBroker or type(broker.store) is not VaultStore
            or getattr(broker.describe, "__func__", None) is not _BROKER_DESCRIBE
            or getattr(broker.store.row, "__func__", None) is not _STORE_ROW):
        raise ExecutorUnavailable("action_account_identity_unavailable")
    refs = ((client.CLIENT_SECRET_REF, client.REFRESH_TOKEN_REF)
            if service in {"calendar", "gmail"} else (client._credential_ref,))
    descriptors = []
    for ref in refs:
        metadata = broker.describe(ref)
        if metadata is None:
            raise ExecutorUnavailable("action_account_credentials_missing")
        descriptors.append({"ref": ref, "version": metadata.get("version"),
                            "rotated_at": metadata.get("rotated_at", ""),
                            "status": metadata.get("status")})
        if service in {"calendar", "gmail"} and metadata.get("oauth_client_id"):
            descriptors[-1]["oauth_client_id"] = metadata["oauth_client_id"]
    return descriptors


def _identity(service, client, *, portal_id="", session_id=""):
    if service == "portal":
        if (not _original(client, PortalCapabilities, _PORTAL_METHODS)
                or not _original(client.client, PortalClient, _PORTAL_CLIENT_METHODS)
                or PR.REDUCERS != _PORTAL_REDUCERS):
            raise ExecutorUnavailable("action_native_client_unavailable")
        binding = BINDINGS.get(portal_id)
        if (type(binding) is not PortalBinding or binding.portal_id != portal_id
                or not re.fullmatch(r"ps-[0-9]+-[0-9]+", session_id)):
            raise ExecutorUnavailable("action_portal_binding_unavailable")
        path = client.client.socket_path
        try:
            socket_stat = os.stat(path, follow_symlinks=False)
        except OSError:
            raise ExecutorUnavailable("action_portal_worker_unavailable") from None
        if not os.path.isabs(path) or not stat.S_ISSOCK(socket_stat.st_mode):
            raise ExecutorUnavailable("action_portal_worker_unavailable")
        return {"service": service, "socket": os.path.realpath(path),
                "socket_identity": [socket_stat.st_dev, socket_stat.st_ino, socket_stat.st_uid],
                "repo_root": os.path.realpath(client.client.repo_root),
                "portal_id": portal_id, "session_id": session_id,
                "binding_digest": _digest(binding.as_data())}
    if service not in _NATIVE or not _original(client, _NATIVE[service][0], _NATIVE_METHODS[service]):
        raise ExecutorUnavailable("action_native_client_unavailable")
    metadata = _credentials(client, service)
    if service in {"calendar", "gmail"}:
        if (not client._client_id or GC._API != "https://www.googleapis.com/calendar/v3"
                or GM._API != "https://gmail.googleapis.com/gmail/v1/users/me"):
            raise ExecutorUnavailable("action_native_configuration_changed")
        if service == "gmail" and getattr(client, "_mime", None) is not _NATIVE_MIME:
            raise ExecutorUnavailable("action_native_configuration_changed")
        return {"service": service, "client_id": metadata[0].get("oauth_client_id", client._client_id),
                "resource": client.calendar_id if service == "calendar" else "me",
                "credentials": metadata}
    host = urlsplit(client.base).hostname or ""
    try:
        local = ipaddress.ip_address(host).is_private
    except ValueError:
        local = host == "localhost" or host.endswith(".local")
    if not local or urlsplit(client.base).scheme not in {"http", "https"}:
        raise ExecutorUnavailable("action_local_ha_required")
    return {"service": service, "base": client.base, "credentials": metadata}


def account_identity(service, client, *, portal_id="", session_id=""):
    """Opaque configured account identity, rotated with the vault credential."""
    return service + "-" + _digest(_identity(service, client, portal_id=portal_id, session_id=session_id))[:32]


def _moment(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("action_absolute_time_required")
    return moment


def _event_projection(event):
    return {"event_id": event.event_id, "summary": event.summary,
            "start": event.start.isoformat(), "end": event.end.isoformat(),
            "all_day": bool(event.all_day), "description": event.description,
            "location": event.location}


def _event_matches(event, desired):
    if event is None:
        return False
    time_matches = ((event.start.date() == _moment(desired["start"]).date()
                     and event.end.date() == _moment(desired["end"]).date()) if desired["all_day"] else
                    (event.start == _moment(desired["start"]) and event.end == _moment(desired["end"])))
    return (event.summary == desired["summary"] and time_matches
            and bool(event.all_day) == desired["all_day"]
            and event.description == desired["description"]
            and event.location == desired["location"])


def _draft_projection(stored):
    message = _message_of(stored)
    payload = (stored.get("message") or stored).get("payload") or {}
    headers = payload.get("headers") or []
    recipients = {}
    for kind in ("to", "cc", "bcc", "from"):
        values = [GM._decode_header(h.get("value", "")) for h in headers
                  if str(h.get("name", "")).lower() == kind]
        recipients[kind] = [address for _, address in getaddresses(values)]
    forbidden_headers = any(str(h.get("name", "")).lower().startswith("resent-")
                            or str(h.get("name", "")).lower() == "reply-to" for h in headers)
    subjects = [GM._decode_header(h.get("value", "")) for h in headers
                if str(h.get("name", "")).lower() == "subject"]
    reply_ids = [h.get("value", "") for h in headers if str(h.get("name", "")).lower() == "in-reply-to"]
    # The existing display parser truncates/sanitizes. Proof may only compare
    # complete native plain text, not its shortened display projection.
    raw_body = (payload.get("body") or {}).get("data")
    complete_body = None
    if (payload.get("mimeType") == "text/plain" and not payload.get("parts")
            and isinstance(raw_body, str) and len(raw_body) <= 65536):
        try:
            complete_body = base64.b64decode(raw_body + "=" * (-len(raw_body) % 4),
                altchars=b"-_", validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            pass
    return {"to": extract_address(message.to), "subject": subjects[0] if len(subjects) == 1 else "",
            "body": complete_body if complete_body is not None else message.body,
            "body_complete": complete_body is not None,
            "thread_id": message.thread_id, "recipients": recipients,
            "in_reply_to": reply_ids,
            "forbidden_headers": forbidden_headers,
            "attachments": list(message.attachment_names)}


def _mail_matches(actual, desired):
    return (actual["to"] == desired["to"] and actual["subject"] == desired["subject"]
            and _normalized(actual["body"]) == _normalized(desired["body"])
            and actual["body_complete"] and not actual["attachments"]
            and actual["recipients"]["to"] == [desired["to"]]
            and not actual["recipients"]["cc"] and not actual["recipients"]["bcc"]
            and not actual["forbidden_headers"])


class TaskServiceAction:
    def __init__(self, ledger, calendar=None, gmail=None, ha=None, exposure=None, portals=None,
                 security_entities=frozenset(), control_plane=None):
        from solvio.agent_runtime import action_account_rebinding as AB
        self.ledger = ledger
        self.calendar, self.gmail, self.ha = calendar, gmail, ha
        self.exposure, self.portals = exposure, portals
        self.control_plane = control_plane
        self.security_entities = frozenset(security_entities)
        self._priced = {}
        AB.initialize(ledger)

    def __call__(self, arguments):
        raise CapabilityRefused("action_task_contract_required")

    def accounts(self):
        result = []
        for service in ("calendar", "gmail", "ha"):
            client = getattr(self, service)
            if client is None:
                continue
            try:
                identity = account_identity(service, client)
            except ExecutorUnavailable:
                continue
            ref = client.REFRESH_TOKEN_REF if service in {"calendar", "gmail"} else client._credential_ref
            metadata = client._broker.describe(ref) or {}
            fallback = {"calendar": "Google-Kalender", "gmail": "Gmail", "ha": "Home Assistant"}[service]
            label = metadata.get("account_label") or fallback
            label = str(label).strip()[:120] or fallback
            result.append({"service": service, "account": identity, "label": label,
                           "resource": client.calendar_id if service == "calendar" else
                           "me" if service == "gmail" else "configured_home"})
        return result

    def resources(self, spec, arguments, task_step):
        from solvio.agent_runtime import action_contract as AC
        from solvio.agent_runtime import action_account_rebinding as AB, cost_dispatch as CD
        from solvio.agent_runtime.task_start_service import TaskStepAuthority
        if (spec != SPEC or type(task_step) is not TaskStepAuthority
                or not _original(self, TaskServiceAction, _TASK_ACTION_SERVICE_METHODS)):
            raise CapabilityRefused("action_task_contract_required")
        bound = AC.for_run(self.ledger, task_step.run_id)
        if bound is None or bound.task_id != task_step.task_id or bound.grant_reference != task_step.reference:
            raise CapabilityRefused("action_task_binding_changed")
        action = AC.get_action(self.ledger, task_step.run_id, arguments=arguments)
        service = action["service"]
        if service not in {"calendar", "gmail", "ha", "portal"}:
            raise ExecutorUnavailable("action_service_not_connected")
        client = self.portals if service == "portal" else getattr(self, service)
        native_args = action["target"] if service == "portal" else {}
        connect = service == 'portal' and action['operation'] == 'connect'
        if connect:
            from solvio.agent_runtime import portal_connection as PC
            connection = PC.checked(self, task_step.run_id)
            identity = json.loads(connection['native_json'])
            expected_account = PC.account_for(identity)
        else:
            identity = _identity(service, client, **native_args)
            expected_account = account_identity(service, client, **native_args)
        account = AB.resolve_account(self.ledger, task_step.run_id, action["action_id"])
        if account.account != expected_account:
            raise CapabilityRefused("action_account_binding_changed")
        if service == "calendar" and action["target"]["calendar_id"] != client.calendar_id:
            raise CapabilityRefused("action_calendar_binding_changed")
        if service == "gmail" and action["target"]["mailbox"] != "me":
            raise CapabilityRefused("action_mailbox_binding_changed")
        if service == "ha" and (not _original(self.exposure, HAExposure, _EXPOSURE_METHODS)
                                or self.exposure.ha is not client):
            raise ExecutorUnavailable("action_ha_exposure_unavailable")
        resources = {"contract": "solvio:native-action:v1", "action": action,
                     "account_binding": _digest(identity), "arguments": dict(arguments),
                     "account_rebinding": {"account": account.account, "reference": account.reference,
                                           "digest": account.digest},
                     "task_id": task_step.task_id, "run_id": task_step.run_id,
                     "grant_reference": task_step.reference}
        if service == "gmail" and action["operation"] == "compose_draft":
            composed = _TASK_ACTION_SERVICE_METHODS["_composition"](self, action, task_step.run_id)
            resources["composition"] = {"reference": composed.reference, "content_digest": composed.content_digest}
        if service == "ha":
            resources["security_entities"] = sorted(self.security_entities)
        # An ephemeral quote cache conveys no authority or execution claim. The
        # router and execute re-read the durable contract before the actual call.
        invocation = CD.ServiceInvocation.bind(capability=SPEC.name, version=SPEC.version,
            service="native.task-action", operation="execute", arguments=arguments, resources=resources)
        self._priced[invocation.request_digest] = (service, identity, account, task_step.run_id, action["action_id"], resources.get("composition"))
        while len(self._priced) > 128:
            self._priced.pop(next(iter(self._priced)))
        return resources

    def quote(self, service, invocation):
        from solvio.agent_runtime import action_account_rebinding as AB, cost_dispatch as CD
        from solvio.agent_runtime.costs import CostEvidence
        entry = self._priced.get(invocation.request_digest)
        if (service != "native.task-action" or invocation.capability != SPEC.name
                or invocation.version != SPEC.version or invocation.operation != "execute" or entry is None):
            raise CapabilityRefused("action_cost_binding_invalid")
        kind, identity, account, run_id, action_id, composition = entry

        def current():
            try:
                if identity.get('operation') == 'portal.connect':
                    from solvio.agent_runtime import portal_connection as PC
                    native = (json.loads(PC.checked(self, run_id)['native_json']) == identity
                              and AB.resolve_account(self.ledger, run_id, action_id) == account)
                else:
                    target = {k: identity[k] for k in ("portal_id", "session_id")} if kind == "portal" else {}
                    native = (_identity(kind, self.portals if kind == "portal" else getattr(self, kind), **target) == identity
                        and AB.resolve_account(self.ledger, run_id, action_id) == account)
                if native and composition is not None:
                    from solvio.agent_runtime import action_contract as AC
                    bound = AC.for_run(self.ledger, run_id)
                    if bound is None:
                        raise ValueError("action_task_binding_changed")
                    action = AC.get_action(self.ledger, run_id, arguments=bound.action_arguments(action_id))
                    composed = _TASK_ACTION_SERVICE_METHODS["_composition"](self, action, run_id)
                    native = {"reference": composed.reference, "content_digest": composed.content_digest} == composition
            except (ExecutorUnavailable, CapabilityRefused, ValueError):
                return False
            return native and (kind in {"ha", "portal"} or GOOGLE_POLICY_CHECKED <= _today() <= GOOGLE_POLICY_EXPIRES)

        if not current():
            return CD.CostQuote()
        # Portal status is the fixed local Unix READ + DOM readout: no navigation,
        # login, form submission, model invocation or external paid tool call.
        evidence = (CostEvidence('free_local', identity['cost_contract']) if identity.get('operation') == 'portal.connect' else
            CostEvidence("free_local", "core:bound-portal-unix-read:v1") if kind == "portal" else
            CostEvidence("free_local", "core:configured-local-ha-rest:v1") if kind == "ha" else
            CostEvidence("included_no_extra_charge", GOOGLE_POLICY_REFERENCE))
        return CD.CostQuote(0, evidence, validate_before_dispatch=current)

    async def execute(self, arguments, task_step):
        from solvio.agent_runtime import action_contract as AC
        from solvio.agent_runtime import cost_dispatch as CD
        receipt_ref = "core:task-action:" + task_step.step_id
        try:
            resources = self.resources(SPEC, arguments, task_step)
            bound = AC.for_run(self.ledger, task_step.run_id)
            action = resources["action"]
        except (ValueError, CapabilityRefused, ExecutorUnavailable):
            return CD.ServiceOutcome("not_dispatched", reason="action_binding_changed", receipt_ref=receipt_ref)
        if not AC.claim_action(self.ledger, bound, action["action_id"], task_step.step_id):
            return CD.ServiceOutcome("not_dispatched", reason="action_already_claimed", receipt_ref=receipt_ref)
        state = {"effect_started": False}
        try:
            # Price is checked again here: direct calls cannot bypass its expiry.
            if action["service"] in {"calendar", "gmail"} and not (
                    GOOGLE_POLICY_CHECKED <= _today() <= GOOGLE_POLICY_EXPIRES):
                raise CapabilityRefused("action_cost_evidence_expired")
            from solvio.secret_vault import context as SC
            native_capability = (_SECRET_CAPABILITIES.get((action["service"], action["operation"]))
                or ("ha_turn_on" if action["payload"]["state"] == "on" else "ha_turn_off"))
            # Closed mapping from the checked action, not model authority. All
            # origin, principal, background and presence restrictions survive.
            with SC.bound(replace(SC.current(), capability=native_capability)):
                if action["service"] == "calendar":
                    native_id, observed = await self._calendar(action, resources, state)
                elif action["service"] == "gmail":
                    native_id, observed = await self._gmail(action, resources, state)
                elif action["service"] == "portal":
                    if action['operation'] == 'connect':
                        from solvio.agent_runtime import portal_connection as PC
                        await PC.prepare(self, task_step)
                        # Stage 1/2 deliberately has no login executor. A real
                        # preparation is never returned as a connected action.
                        raise CapabilityRefused('portal_login_not_wired')
                    native_id, observed = await self._portal(action, resources, state)
                else:
                    native_id, observed = await self._ha(action, resources, state)
            if "composition" in resources:
                observed = dict(observed, composition_reference=resources["composition"]["reference"],
                                content_digest=resources["composition"]["content_digest"])
            selected = resources["account_rebinding"]
            observed = dict(observed, actual_account=selected["account"],
                account_rebind_reference=selected["reference"], account_rebind_digest=selected["digest"])
            receipt = AC.record_outcome(self.ledger, bound, action["action_id"], task_step.step_id,
                status="completed", receipt={"native_id": native_id, "observed": observed})
            confirmed = observed.get("confirmed") is True
            return CD.ServiceOutcome("completed", ok=confirmed,
                reason="" if confirmed else "action_effect_unconfirmed", receipt_ref=receipt_ref,
                data={"action_id": action["action_id"], "service": action["service"],
                      "operation": action["operation"], "confirmed": confirmed, "receipt": receipt,
                      "content_trust": "untrusted_service"})
        except (CalendarAuthError, GM.GmailAuthError, SecretDenied, SecretUnavailable, HA.ExecutorTokenMissing):
            # Auth rejection establishes no write; a readback failure after a
            # write remains unknown and never turns into permission to repeat.
            status = "unknown" if state["effect_started"] else "not_dispatched"
            AC.record_outcome(self.ledger, bound, action["action_id"], task_step.step_id,
                              status=status, reason="action_account_access_required")
            return CD.ServiceOutcome(status, reason="action_account_access_required", receipt_ref=receipt_ref)
        except (CapabilityRefused, ExecutorUnavailable, ValueError) as exc:
            status = "unknown" if state["effect_started"] else "not_dispatched"
            AC.record_outcome(self.ledger, bound, action["action_id"], task_step.step_id, status=status)
            reason = exc.reason if isinstance(exc, CapabilityRefused) else "action_precondition_changed"
            return CD.ServiceOutcome(status, reason=reason, receipt_ref=receipt_ref)
        except BaseException as exc:
            AC.record_outcome(self.ledger, bound, action["action_id"], task_step.step_id, status="unknown")
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            return CD.ServiceOutcome("unknown", reason="action_outcome_unknown")

    def _unchanged(self, action, resources):
        from solvio.agent_runtime import action_contract as AC
        from solvio.agent_runtime import action_account_rebinding as AB
        # Resolves through the actual current Grant/Task state on every call.
        # No await follows this check before a physical write is entered.
        if (not _original(self, TaskServiceAction, _TASK_ACTION_SERVICE_METHODS)
                or AC.get_action(self.ledger, resources["run_id"], arguments=resources["arguments"]) != action):
            raise CapabilityRefused("action_task_binding_changed")
        if action["service"] in {"calendar", "gmail"} and not (
                GOOGLE_POLICY_CHECKED <= _today() <= GOOGLE_POLICY_EXPIRES):
            raise CapabilityRefused("action_cost_evidence_expired")
        service = action["service"]
        client = self.portals if service == "portal" else getattr(self, service)
        native_args = action["target"] if service == "portal" else {}
        account = AB.resolve_account(self.ledger, resources["run_id"], action["action_id"])
        if {"account": account.account, "reference": account.reference, "digest": account.digest} != resources["account_rebinding"]:
            raise CapabilityRefused("action_account_rebinding_changed")
        if service == 'portal' and action['operation'] == 'connect':
            from solvio.agent_runtime import portal_connection as PC
            actual_identity = PC.native(self, action['target']['portal_id'])
        else:
            actual_identity = _identity(service, client, **native_args)
        if _digest(actual_identity) != resources["account_binding"]:
            raise CapabilityRefused("action_account_binding_changed")
        if service == "gmail" and action["operation"] == "compose_draft":
            composed = _TASK_ACTION_SERVICE_METHODS["_composition"](self, action, resources["run_id"])
            if {"reference": composed.reference, "content_digest": composed.content_digest} != resources.get("composition"):
                raise CapabilityRefused("action_draft_composition_changed")
        if action["service"] == "ha" and sorted(self.security_entities) != resources["security_entities"]:
            raise CapabilityRefused("action_ha_security_binding_changed")

    def _composition(self, action, run_id):
        from solvio.agent_runtime import action_contract as AC
        from solvio.agent_runtime.action_draft_composition import read_composed
        bound = AC.for_run(self.ledger, run_id)
        composed = read_composed(self.ledger, bound, action)
        if composed is None:
            raise ExecutorUnavailable("action_draft_not_composed")
        AC._id(composed.reference, "composition_reference")
        if type(composed.content_digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", composed.content_digest):
            raise CapabilityRefused("action_draft_composition_changed")
        AC._text(composed.subject, "subject", limit=500)
        AC._text(composed.body, "body", limit=8000)
        if "\n" in composed.subject or "\r" in composed.subject:
            raise CapabilityRefused("action_draft_subject_invalid")
        return composed

    async def _portal(self, action, resources, state):
        from solvio.secret_vault import context as SC
        from solvio.agent_runtime.task_authority import TaskAuthority
        from solvio.agent_runtime.steps import agent_principal
        task = self.ledger.get_task(self.ledger.get_run(resources["run_id"]).task_id)
        grant = TaskAuthority(self.ledger).for_run(resources["run_id"])
        owner = task.created_principal
        if (not owner or grant is None or grant.authorizer != owner
                or grant.reference != resources["grant_reference"]
                or SC.current().principal not in {owner, agent_principal(resources["run_id"])}):
            raise CapabilityRefused("action_portal_owner_required")
        target = action["target"]
        binding = BINDINGS[target["portal_id"]]
        # Check the actual installed worker before using any authenticated page.
        # This is the existing native build handshake, not a model assertion.
        try:
            await self.portals.client.verify_build()
        except (WorkerBuildMismatch, PortalUnavailable) as exc:
            raise ExecutorUnavailable("action_portal_worker_unavailable") from exc
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        try:
            # Only this native read uses the verified delegating owner. The
            # outer router keeps the distinct background agent identity.
            with SC.bound(replace(SC.current(), principal=owner)):
                result = await self.portals.status({"session": target["session_id"]})
        except CapabilityDeclined as exc:
            if exc.reason in {"session_expired", "unknown_session"}:
                raise SecretUnavailable("portal_session_access_required") from exc
            raise CapabilityRefused("action_portal_read_unavailable") from exc
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        native = result.get("session_binding")
        expected = {"session_id": target["session_id"], "portal_id": target["portal_id"],
                    "owner_principal": owner,
                    "binding_digest": _digest(binding.as_data()), "authenticated": True}
        if not isinstance(native, dict) or any(native.get(k) != v for k, v in expected.items() if k != "authenticated"):
            raise CapabilityRefused("action_portal_session_binding_changed")
        if native.get("authenticated") is not True:
            raise SecretUnavailable("portal_session_access_required")
        if (result.get("session") != target["session_id"] or not binding.allows(result.get("url", ""))
                or result.get("content_trust") != "untrusted_web"):
            raise CapabilityRefused("action_portal_result_unbound")
        return target["session_id"], {"confirmed": True, "portal_id": target["portal_id"],
            "session_id": target["session_id"], "status": result, "content_trust": "untrusted_web"}

    async def _calendar(self, action, resources, state):
        client, operation = self.calendar, action["operation"]
        target, payload = action["target"], action["payload"]
        await client._token()
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        if operation == "list":
            events = await client.list_events(_moment(payload["start"]), _moment(payload["end"]))
            return target["calendar_id"], {"confirmed": len(events) < 250,
                "possibly_truncated": len(events) >= 250, "events": [_event_projection(e) for e in events]}
        if operation == "create":
            native_id = "solvio" + _digest({"task": resources["task_id"], "action": action})[:40]
            state["effect_started"] = True
            await client.create_event(summary=payload["summary"], start=_moment(payload["start"]),
                end=_moment(payload["end"]), all_day=payload["all_day"],
                description=payload["description"], location=payload["location"], client_id=native_id)
        else:
            native_id = target["event_id"]
            before = await client.get_event(native_id)
            if before is None and operation == "update":
                raise CapabilityRefused("action_event_missing")
            if operation == "update" and before.all_day != payload["all_day"]:
                raise CapabilityRefused("action_calendar_event_kind_change_not_supported")
            await client._token()
            _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
            state["effect_started"] = True
            if operation == "delete":
                await client.delete_event(native_id)
            elif operation == "update":
                if payload["all_day"]:
                    # Same original REST client; patch Events date fields using
                    # the native create port's all-day representation.
                    # https://developers.google.com/workspace/calendar/api/v3/reference/events/patch
                    await client._request("PATCH", f"/calendars/{client.calendar_id}/events/{native_id}",
                        params={"sendUpdates": "none"}, json_body={"summary": payload["summary"],
                            "description": payload["description"], "location": payload["location"],
                            "start": {"date": _moment(payload["start"]).date().isoformat()},
                            "end": {"date": _moment(payload["end"]).date().isoformat()}})
                else:
                    await client.update_event(native_id, summary=payload["summary"],
                        start=_moment(payload["start"]), end=_moment(payload["end"]),
                        description=payload["description"], location=payload["location"])
            else:
                raise CapabilityRefused("action_operation_unknown")
        actual = await client.get_event(native_id)
        confirmed = actual is None if operation == "delete" else _event_matches(actual, payload)
        return native_id, {"confirmed": confirmed, "event": None if actual is None else _event_projection(actual)}

    async def _gmail(self, action, resources, state):
        client, operation = self.gmail, action["operation"]
        target, payload = action["target"], action["payload"]
        await client._token()
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        own = await client.profile()
        sender = extract_address(str(own.get("emailAddress", "")))
        if not sender:
            raise CapabilityRefused("action_mail_account_unverified")
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        if operation == "search":
            messages = await client.search(payload["query"], limit=payload["limit"])
            return "me", {"confirmed": True, "messages": [m.as_data(with_body=True) for m in messages]}
        if operation in {"create_draft", "compose_draft"}:
            if operation == "compose_draft":
                composed = _TASK_ACTION_SERVICE_METHODS["_composition"](self, action, resources["run_id"])
                payload = {"subject": composed.subject, "body": composed.body, "thread_id": "", "in_reply_to": ""}
            reply = target.get("reply_to_message", "")
            if reply:
                raw_original = await client._request("GET", "/messages/" + reply, params={"format": "full"})
                original = None if raw_original is None else GM.message_from_api(raw_original)
                headers = ((raw_original or {}).get("payload") or {}).get("headers") or []
                ids = [h.get("value", "") for h in headers if str(h.get("name", "")).lower() == "message-id"]
                senders = [address for _, address in getaddresses([GM._decode_header(h.get("value", ""))
                    for h in headers if str(h.get("name", "")).lower() == "from"])]
                if (original is None or senders != [target["to"]]
                        or original.thread_id != payload["thread_id"]
                        or ids != [payload["in_reply_to"]]):
                    raise CapabilityRefused("action_reply_binding_changed")
            elif payload["thread_id"] or payload["in_reply_to"]:
                raise CapabilityRefused("action_reply_binding_missing")
            await client._token()
            _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
            state["effect_started"] = True
            created = await client.create_draft(to=target["to"], subject=payload["subject"],
                body=payload["body"], thread_id=payload["thread_id"], in_reply_to=payload["in_reply_to"])
            native_id = created.get("id", "")
            if not native_id:
                raise ValueError("action_draft_id_missing")
            stored = await client.get_draft(native_id)
            if stored is None:
                return native_id, {"confirmed": False, "draft": None, "sent": False}
            actual = _draft_projection(stored)
            confirmed = (_mail_matches(actual, {"to": target["to"], **payload})
                         and actual["recipients"]["from"] in ([], [sender])
                         and actual["in_reply_to"] == ([payload["in_reply_to"]] if payload["in_reply_to"] else [])
                         and (not payload["thread_id"] or actual["thread_id"] == payload["thread_id"]))
            return native_id, {"confirmed": confirmed, "draft": actual, "sent": False}
        if operation != "send_draft":
            raise CapabilityRefused("action_operation_unknown")
        # ADR-0041: kein Mailversand aus strukturierten Auftraegen (siehe
        # action_contract.prepare) — auch nicht aus einem frueher gebundenen Vertrag.
        raise CapabilityRefused("action_mail_send_requires_face_id")
        # Der fruehere Versandweg (aktualisieren und senden mit Nachlesen) steht in der
        # Geschichte vor ADR-0041; er kommt zurueck, wenn die Freigabe je Auftrag den
        # Inhalt bindet.

    async def _ha(self, action, resources, state):
        with self.ha._auth():
            pass
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        entity_id = action["target"]["entity_id"]
        entities = await self.exposure.boundary(force=True)
        entity = entities.get(entity_id)
        if entity is None or not entity.executable:
            raise CapabilityRefused("action_ha_target_not_exposed")
        # Reuse the same security classification as ordinary HA capabilities.
        from solvio.capabilities.policy import ActionClass
        if entity.action_class(self.security_entities) is not ActionClass.HA_NORMAL:
            raise CapabilityRefused("action_ha_target_security_sensitive")
        payload, operation = action["payload"], action["operation"]
        if operation == "set_brightness":
            if entity.domain != "light":
                raise CapabilityRefused("action_ha_not_light")
            data = {"entity_id": entity_id, "brightness_pct": payload["brightness_pct"]}
            service = "turn_on"
        elif operation == "set_state":
            data = {"entity_id": entity_id}
            service = "turn_on" if payload["state"] == "on" else "turn_off"
        else:
            raise CapabilityRefused("action_operation_unknown")
        if not await self.exposure.is_exposed(entity_id):
            raise CapabilityRefused("action_ha_target_not_exposed")
        with self.ha._auth():
            pass
        _TASK_ACTION_SERVICE_METHODS["_unchanged"](self, action, resources)
        state["effect_started"] = True
        await self.ha.call_service(entity.domain, service, data)
        expected_state = "on" if service == "turn_on" else "off"
        actual = {}
        confirmed = False
        for delay in (0.25, 0.6):
            await asyncio.sleep(delay)
            actual = await self.ha.state(entity_id)
            confirmed = actual.get("entity_id") == entity_id and actual.get("state") == expected_state
            if operation == "set_brightness":
                wanted = round(payload["brightness_pct"] * 255 / 100)
                brightness = (actual.get("attributes") or {}).get("brightness")
                confirmed = confirmed and type(brightness) in {int, float} and abs(brightness - wanted) <= 1
            if confirmed:
                break
        return entity_id, {"confirmed": bool(confirmed), "state": actual.get("state"),
            "brightness": (actual.get("attributes") or {}).get("brightness")}


_TASK_ACTION_SERVICE_METHODS = {name: getattr(TaskServiceAction, name)
    for name in ("resources", "quote", "execute", "_unchanged", "_calendar", "_gmail", "_ha", "_portal", "_composition", "__call__")}
