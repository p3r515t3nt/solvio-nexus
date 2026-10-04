"""Closed authenticated action resources and durable effect identities.

This module does not authenticate callers or execute services. The verified
task entrance owns admission, the existing router owns policy/cost admission,
and service adapters own native readback. All rows live in the AgentRunLedger.
An action identity survives changed plan sequence numbers and process restarts.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import time

from solvio.agent_runtime import store as S, requirements as RQ
from solvio.agent_runtime.task_authority import (
    CapabilityGrant, TaskAuthority, _canonical, _digest as authority_digest,
    _grant_binding, _task_fingerprint,
)

ACTION_CAPABILITY = CAPABILITY = "task_service_action"
VERSION = 1
MAX_ACTIONS = 5
_FIELDS = {"action_id", "service", "operation", "account", "target", "payload"}
_ARGS = {"resource_id", "contract_digest", "action_id"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,127}\Z")
_ACTION_ID = re.compile(r"[a-z][a-z0-9_]{0,15}\Z")
READ_OPERATIONS = frozenset({("calendar", "list"), ("gmail", "search"), ("portal", "status")})
READ_ONLY_OPERATIONS = READ_OPERATIONS
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_action_contracts (
 resource_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES agent_tasks(task_id),
 run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id), contract_digest TEXT NOT NULL,
 request_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_action_claims (
 task_id TEXT NOT NULL, action_id TEXT NOT NULL, run_id TEXT NOT NULL,
 resource_id TEXT NOT NULL REFERENCES agent_action_contracts(resource_id),
 contract_digest TEXT NOT NULL, grant_reference TEXT NOT NULL, step_id TEXT NOT NULL UNIQUE,
 action_digest TEXT NOT NULL, dispatch_binding TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('claimed','completed','unknown','not_dispatched')),
 receipt_json TEXT NOT NULL DEFAULT '', receipt_digest TEXT NOT NULL DEFAULT '',
 requirement_id TEXT NOT NULL DEFAULT '', requirements_digest TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(task_id,action_id)
);
CREATE TABLE IF NOT EXISTS agent_action_attempt_receipts (
 task_id TEXT NOT NULL, action_id TEXT NOT NULL, run_id TEXT NOT NULL,
 resource_id TEXT NOT NULL REFERENCES agent_action_contracts(resource_id),
 contract_digest TEXT NOT NULL, grant_reference TEXT NOT NULL, step_id TEXT PRIMARY KEY,
 action_digest TEXT NOT NULL, dispatch_binding TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status='not_dispatched'),
 receipt_json TEXT NOT NULL, receipt_digest TEXT NOT NULL,
 requirement_id TEXT NOT NULL, requirements_digest TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""
_CLAIM_COLUMNS = ("task_id,action_id,run_id,resource_id,contract_digest,grant_reference,"
    "step_id,action_digest,dispatch_binding,status,receipt_json,receipt_digest,"
    "requirement_id,requirements_digest,created_at,updated_at")


def initialize(ledger):
    with ledger._open() as connection:
        connection.executescript(SCHEMA)


def _keys(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("invalid_action_" + label)


def _text(value, label, *, limit=2000, empty=False):
    if (type(value) is not str or len(value) > limit or (not empty and not value.strip())
            or any(ord(c) < 32 and c not in "\n\t" for c in value)):
        raise ValueError("invalid_action_" + label)
    return value


def _id(value, label, *, empty=False):
    if empty and value == "":
        return value
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid_action_" + label)
    return value


def _instant(value):
    if type(value) is not str or len(value) > 40 or "T" not in value:
        raise ValueError("absolute_action_time_required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_action_time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("absolute_action_time_required")
    return parsed


def _time_range(payload, *, event=False):
    start, end = _instant(payload["start"]), _instant(payload["end"])
    if end <= start:
        raise ValueError("invalid_action_time_range")
    if event:
        if type(payload["all_day"]) is not bool:
            raise ValueError("invalid_action_all_day")
        if payload["all_day"] and any((v.hour, v.minute, v.second, v.microsecond) != (0, 0, 0, 0)
                                      for v in (start, end)):
            raise ValueError("all_day_requires_midnight")


def _email(value):
    if (type(value) is not str or len(value) > 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?", value)):
        raise ValueError("invalid_action_recipient")


def _validate_action(action):
    _keys(action, _FIELDS, "fields")
    if type(action["action_id"]) is not str or not _ACTION_ID.fullmatch(action["action_id"]):
        raise ValueError("invalid_action_id")
    _id(action["account"], "account")
    service, operation, target, payload = (action[k] for k in ("service", "operation", "target", "payload"))
    if type(service) is not str or type(operation) is not str:
        raise ValueError("invalid_action_operation")
    if service == "calendar" and operation in {"create", "update", "delete", "list"}:
        _keys(target, {"calendar_id", "event_id"} if operation in {"update", "delete"} else {"calendar_id"}, "target")
        for key, value in target.items():
            _id(value, key)
        if operation in {"create", "update"}:
            _keys(payload, {"summary", "start", "end", "all_day", "description", "location"}, "payload")
            _text(payload["summary"], "summary", limit=500)
            _text(payload["description"], "description", limit=4000, empty=True)
            _text(payload["location"], "location", limit=500, empty=True)
            _time_range(payload, event=True)
        elif operation == "list":
            _keys(payload, {"start", "end"}, "payload")
            _time_range(payload)
        else:
            _keys(payload, set(), "payload")
    elif service == "gmail" and operation in {"create_draft", "compose_draft", "send_draft", "search"}:
        targets = {"create_draft": {"mailbox", "to", "reply_to_message"},
                   "compose_draft": {"mailbox", "to"},
                   "send_draft": {"mailbox", "draft_id"}, "search": {"mailbox"}}
        _keys(target, targets[operation], "target")
        if target["mailbox"] != "me":
            raise ValueError("invalid_action_mailbox")
        if operation == "create_draft":
            _email(target["to"])
            _id(target["reply_to_message"], "reply_to_message", empty=True)
            _keys(payload, {"subject", "body", "thread_id", "in_reply_to"}, "payload")
            _id(payload["thread_id"], "thread_id", empty=True)
            reply = payload["in_reply_to"]
            if type(reply) is not str or len(reply) > 254 or (reply and not re.fullmatch(r"<?[A-Za-z0-9._:@+\-]+>?", reply)):
                raise ValueError("invalid_action_reply_header")
        elif operation == "compose_draft":
            _email(target["to"])
            _keys(payload, {"instruction"}, "payload")
            _text(payload["instruction"], "instruction", limit=4000)
        elif operation == "send_draft":
            _id(target["draft_id"], "draft_id")
            _keys(payload, {"to", "subject", "body"}, "payload")
            _email(payload["to"])
        else:
            _keys(payload, {"query", "limit"}, "payload")
            _text(payload["query"], "query", limit=1000)
            if type(payload["limit"]) is not int or not 1 <= payload["limit"] <= 50:
                raise ValueError("invalid_action_limit")
        if operation in {"create_draft", "send_draft"}:
            _text(payload["subject"], "subject", limit=500)
            if "\n" in payload["subject"] or "\r" in payload["subject"]:
                raise ValueError("invalid_action_subject")
            _text(payload["body"], "body", limit=8000)
    elif service == "ha" and operation in {"set_state", "set_brightness"}:
        _keys(target, {"entity_id"}, "target")
        if type(target["entity_id"]) is not str or not re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z0-9_]+", target["entity_id"]):
            raise ValueError("invalid_action_entity")
        if operation == "set_state":
            _keys(payload, {"state"}, "payload")
            if payload["state"] not in ("on", "off"):
                raise ValueError("invalid_action_state")
        else:
            _keys(payload, {"brightness_pct"}, "payload")
            if type(payload["brightness_pct"]) is not int or not 0 <= payload["brightness_pct"] <= 100:
                raise ValueError("invalid_action_brightness")
    elif service == "portal" and operation == "connect":
        _keys(target, {"portal_id"}, "target")
        _id(target['portal_id'], 'portal_id')
        _keys(payload, set(), 'payload')
    elif service == "portal" and operation == "status":
        _keys(target, {"portal_id", "session_id"}, "target")
        for key, value in target.items():
            _id(value, key)
        _keys(payload, set(), "payload")
    else:
        raise ValueError("unsupported_action_operation")


def _validated(value):
    _keys(value, {"actions"}, "request")
    if type(value["actions"]) is not list or not 1 <= len(value["actions"]) <= MAX_ACTIONS:
        raise ValueError("invalid_action_count")
    seen = set()
    for action in value["actions"]:
        _validate_action(action)
        if action["action_id"] in seen:
            raise ValueError("duplicate_action_id")
        seen.add(action["action_id"])
    if (len(value['actions']) != 1 and any((a['service'], a['operation']) == ('portal', 'connect')
                                         for a in value['actions'])):
        raise ValueError('portal_connect_must_be_single_action')
    return _canonical(value)


@dataclass(frozen=True)
class ActionRequest:
    _payload_json: str = field(repr=False)

    def __post_init__(self):
        if type(self._payload_json) is not str or _validated(json.loads(self._payload_json)) != self._payload_json:
            raise ValueError("noncanonical_action_request")

    @classmethod
    def from_payload(cls, payload):
        return cls(_validated(payload))

    @property
    def descriptor(self):
        return json.loads(self._payload_json)

    @property
    def actions(self):
        return tuple(self.descriptor["actions"])


def from_payload(value):
    return ActionRequest.from_payload(value)


def validate_request(value):
    if type(value) is ActionRequest:
        value.__post_init__()
        return value
    return from_payload(value)


def canonical_request(value):
    return validate_request(value).descriptor


def is_read_only(action):
    _validate_action(action)
    return (action["service"], action["operation"]) in READ_OPERATIONS


def _hash(domain, value):
    return hashlib.sha256(domain.encode() + b"\0" + _canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class BoundActions:
    task_id: str
    run_id: str
    resource_id: str
    contract_digest: str
    _request_json: str = field(repr=False)
    grant_reference: str = ""

    @property
    def actions(self):
        return ActionRequest(self._request_json).actions

    @property
    def capability_grant(self):
        return CapabilityGrant(CAPABILITY, VERSION, {
            "resource_id": self.resource_id, "contract_digest": self.contract_digest})

    def action_arguments(self, action_id):
        if action_id not in {a["action_id"] for a in self.actions}:
            raise ValueError("unknown_action_id")
        return dict(self.capability_grant.constraints, action_id=action_id)


def _mail_send_requested(request) -> bool:
    return any(a.get("service") == "gmail" and a.get("operation") == "send_draft"
               for a in request.actions)


def prepare(request, *, task_id, run_id):
    if type(request) is not ActionRequest:
        raise ValueError("typed_action_request_required")
    request.__post_init__()
    if not re.fullmatch(r"at-[a-f0-9]{16}", task_id) or not re.fullmatch(r"ar-[a-f0-9]{16}", run_id):
        raise ValueError("invalid_action_task_identity")
    # ADR-0041: jede Mail, die das Haus verlaesst, sieht der Mensch vorher mit Face ID.
    # Ein strukturierter Auftrag kann das heute nicht zusichern (sein Start kann eine
    # Dashboard-Sitzung sein) — also versendet er keine Mail, bis der Auftragsweg die
    # Freigabe je Auftrag an den Inhalt bindet.
    if _mail_send_requested(request):
        raise ValueError("action_mail_send_requires_face_id")
    digest = _hash("SOLVIO_ACTION_CONTRACT_V1", {
        "version": VERSION, "task_id": task_id, "run_id": run_id, "request": request.descriptor})
    return BoundActions(task_id, run_id, "ac-" + digest[:24], digest, request._payload_json)


def _checked(bound):
    if type(bound) is not BoundActions:
        raise ValueError("typed_bound_actions_required")
    expected = prepare(ActionRequest(bound._request_json), task_id=bound.task_id, run_id=bound.run_id)
    if replace(bound, grant_reference="") != expected:
        raise ValueError("action_binding_changed")
    return expected


def record_prepared(connection, bound, *, now):
    _checked(bound)
    row = connection.execute("SELECT * FROM agent_action_contracts WHERE task_id=? OR run_id=?",
                             (bound.task_id, bound.run_id)).fetchone()
    if row:
        if _read_bound(connection, bound) != bound._request_json:
            raise ValueError("action_contract_already_bound")
        return
    connection.execute("INSERT INTO agent_action_contracts VALUES (?,?,?,?,?,?)", (
        bound.resource_id, bound.task_id, bound.run_id, bound.contract_digest, bound._request_json, now))


def _read_bound(connection, bound):
    _checked(bound)
    row = connection.execute("SELECT * FROM agent_action_contracts WHERE resource_id=?", (bound.resource_id,)).fetchone()
    if row is None or any(row[k] != getattr(bound, k) for k in ("task_id", "run_id", "contract_digest")) or row["request_json"] != bound._request_json:
        raise ValueError("action_contract_binding_changed")
    return row["request_json"]


def _bound_row(row):
    return BoundActions(row["task_id"], row["run_id"], row["resource_id"], row["contract_digest"], row["request_json"])


def check_prepared(ledger, *, task_id, run_id, entries):
    matches = [e for e in entries if e.name == CAPABILITY]
    with ledger._open() as connection:
        row = connection.execute("SELECT * FROM agent_action_contracts WHERE task_id=? OR run_id=?", (task_id, run_id)).fetchone()
        if not matches and row is None:
            return None
        if len(matches) != 1 or row is None:
            raise ValueError("action_grant_resource_mismatch")
        bound = _bound_row(row)
        _read_bound(connection, bound)
        if (bound.task_id, bound.run_id) != (task_id, run_id) or matches[0] != bound.capability_grant:
            raise ValueError("invalid_action_grant")
        return bound


def _with_grant(connection, ledger, bound, *, active):
    _read_bound(connection, bound)
    authority = TaskAuthority.__new__(TaskAuthority)
    authority.ledger, authority.clock = ledger, time.time
    row = connection.execute("SELECT * FROM agent_task_grants WHERE run_id=?", (bound.run_id,)).fetchone()
    task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (bound.task_id,)).fetchone()
    if row is None or task is None or row["task_id"] != bound.task_id:
        raise ValueError("action_not_granted")
    caps = json.loads(row["capabilities"])
    expected = _grant_binding(task_id=bound.task_id, run_id=bound.run_id,
        fingerprint=_task_fingerprint(task), method=row["receipt_method"], receipt=row["receipt_reference"],
        authorizer=row["authorizer"], capabilities=caps, expires_at=row["expires_at"])
    grants = [CapabilityGrant(**c) for c in caps if c["name"] == CAPABILITY]
    if expected != row["binding_digest"] or grants != [bound.capability_grant] or (bound.grant_reference and bound.grant_reference != row["reference"]):
        raise ValueError("action_grant_binding_changed")
    result = replace(bound, grant_reference=row["reference"])
    from solvio.agent_runtime.action_intent import validate_bound
    validate_bound(connection, ledger, result)
    if active:
        verdict = authority._verify(connection, result.grant_reference, CAPABILITY,
            result.action_arguments(result.actions[0]["action_id"]), VERSION,
            task_id=result.task_id, run_id=result.run_id)
        if not verdict.allowed:
            raise ValueError("action_authority_invalid:" + verdict.reason)
    return result


def for_run(ledger, run_id):
    with ledger._open() as connection:
        connection.execute("BEGIN")
        row = connection.execute("SELECT * FROM agent_action_contracts WHERE run_id=?", (run_id,)).fetchone()
        return _with_grant(connection, ledger, _bound_row(row), active=True) if row else None


def get_action(ledger, run_id, *, arguments):
    bound = for_run(ledger, run_id)
    _keys(arguments, _ARGS, "arguments")
    if bound is None or arguments != bound.action_arguments(arguments["action_id"]):
        raise ValueError("action_arguments_changed")
    return next(a for a in bound.actions if a["action_id"] == arguments["action_id"])


def claim_action(ledger, bound, action_id, step_id):
    """CAS one semantic action; only a proven non-dispatch can create an attempt.

    The old immutable receipt is archived in this same transaction before the
    current pointer moves. An earlier non-dispatch never reopens a later
    claimed, completed or unknown attempt, even under a new plan sequence.
    """
    arguments = bound.action_arguments(action_id)
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _with_grant(connection, ledger, bound, active=True)
        step = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (step_id,)).fetchone()
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (bound.run_id,)).fetchone()
        if (step is None or step["run_id"] != bound.run_id or step["kind"] != "capability"
                or step["capability"] != CAPABILITY or step["state"] != "running"
                or step["finished_at"] is not None or run["state"] != S.RUNNING):
            raise ValueError("action_step_binding_invalid")
        effect = authority_digest("SOLVIO_TASK_EFFECT_V1", {"grant": current.grant_reference,
            "grant_binding": connection.execute("SELECT binding_digest FROM agent_task_grants WHERE reference=?", (current.grant_reference,)).fetchone()[0],
            "capability": CAPABILITY, "version": VERSION, "arguments": arguments,
            "task_id": bound.task_id, "run_id": bound.run_id})
        dispatch = authority_digest("SOLVIO_TASK_STEP_DISPATCH_V1", {"effect": effect,
            "step_id": step_id, "seq": step["seq"], "attempt": step["attempt"]})
        if step["dispatch_binding_digest"] != dispatch or step["dispatch_claimed_at"] is None:
            raise ValueError("action_router_claim_required")
        action = next(a for a in current.actions if a["action_id"] == action_id)
        now = time.time()
        prior = connection.execute("SELECT * FROM agent_action_claims WHERE task_id=? AND action_id=?",
                                   (bound.task_id, action_id)).fetchone()
        if prior is not None:
            if prior["step_id"] == step_id or prior["status"] != "not_dispatched":
                return False
            old_step = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (prior["step_id"],)).fetchone()
            if not can_retry_step(connection, ledger, old_step, task_id=bound.task_id,
                    run_id=bound.run_id, capability=CAPABILITY, arguments=arguments, version=VERSION):
                return False
            if connection.execute("SELECT 1 FROM agent_action_attempt_receipts WHERE step_id=?", (step_id,)).fetchone():
                return False
            connection.execute(f"INSERT INTO agent_action_attempt_receipts ({_CLAIM_COLUMNS}) "
                f"SELECT {_CLAIM_COLUMNS} FROM agent_action_claims WHERE task_id=? AND action_id=?",
                (bound.task_id, action_id))
            updated = connection.execute("UPDATE agent_action_claims SET step_id=?,dispatch_binding=?,status='claimed',"
                "receipt_json='',receipt_digest='',requirement_id='',requirements_digest='',created_at=?,updated_at=? "
                "WHERE task_id=? AND action_id=? AND step_id=? AND status='not_dispatched' AND receipt_digest=?",
                (step_id, dispatch, now, now, bound.task_id, action_id, prior["step_id"], prior["receipt_digest"]))
            if updated.rowcount != 1:
                raise ValueError("action_retry_cas_failed")
            from solvio.agent_runtime.action_account_rebinding import bind_dispatch
            bind_dispatch(connection, ledger, current, action, step)
            return True
        cursor = connection.execute("INSERT OR IGNORE INTO agent_action_claims "
            "(task_id,action_id,run_id,resource_id,contract_digest,grant_reference,step_id,action_digest,dispatch_binding,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'claimed',?,?)", (bound.task_id, action_id, bound.run_id,
                bound.resource_id, bound.contract_digest, current.grant_reference, step_id,
                _hash("SOLVIO_ACTION_V1", action), dispatch, now, now))
        if cursor.rowcount:
            from solvio.agent_runtime.action_account_rebinding import bind_dispatch
            bind_dispatch(connection, ledger, current, action, step)
        return bool(cursor.rowcount)


def can_retry_step(connection, ledger, previous_step_row, *, task_id, run_id,
                   capability, arguments, version):
    """Read-only exception for TaskAuthority's prior-step guard, never authority.

    The caller still validates and claims its NEW step. Every prior claimed
    step it considers must have its own canonical non-dispatch receipt, and
    the action's current attempt must also be a proven non-dispatch. This
    permits another bounded authentication retry, not a retry of an effect.
    """
    try:
        _keys(arguments, _ARGS, "arguments")
        if capability != CAPABILITY or version != VERSION or type(version) is not int or previous_step_row is None:
            return False
        previous_id = previous_step_row["step_id"]
        actual_step = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (previous_id,)).fetchone()
        if (actual_step is None or actual_step["run_id"] != run_id or actual_step["kind"] != "capability"
                or actual_step["capability"] != CAPABILITY
                or any(actual_step[k] != previous_step_row[k] for k in ("run_id", "kind", "capability", "seq", "attempt", "dispatch_binding_digest", "dispatch_claimed_at"))):
            return False
        contract = connection.execute("SELECT * FROM agent_action_contracts WHERE run_id=?", (run_id,)).fetchone()
        if contract is None:
            return False
        bound = _with_grant(connection, ledger, _bound_row(contract), active=True)
        if bound.task_id != task_id or arguments != bound.action_arguments(arguments["action_id"]):
            return False
        current = connection.execute("SELECT * FROM agent_action_claims WHERE task_id=? AND action_id=?",
            (task_id, arguments["action_id"])).fetchone()
        if current is None or current["status"] != "not_dispatched":
            return False
        current_receipt = _read_receipt(connection, ledger, current)
        if current_receipt["status"] != "not_dispatched" or current["requirement_id"] or current["requirements_digest"]:
            return False
        for archived in connection.execute("SELECT * FROM agent_action_attempt_receipts WHERE task_id=? AND action_id=?",
                (task_id, arguments["action_id"])).fetchall():
            observed = _read_receipt(connection, ledger, archived)
            if observed["status"] != "not_dispatched" or observed["requirement"] or observed["requirements_digest"]:
                return False
        prior = (current if current["step_id"] == previous_id else connection.execute(
            "SELECT * FROM agent_action_attempt_receipts WHERE step_id=?", (previous_id,)).fetchone())
        if (prior is None or prior["status"] != "not_dispatched" or prior["requirement_id"]
                or prior["requirements_digest"] or prior["action_id"] != arguments["action_id"]
                or prior["task_id"] != task_id or prior["run_id"] != run_id):
            return False
        receipt = _read_receipt(connection, ledger, prior)
        if receipt["status"] != "not_dispatched":
            return False
        grant_row = connection.execute("SELECT binding_digest FROM agent_task_grants WHERE reference=?", (bound.grant_reference,)).fetchone()
        effect = authority_digest("SOLVIO_TASK_EFFECT_V1", {"grant": bound.grant_reference,
            "grant_binding": grant_row[0], "capability": CAPABILITY, "version": VERSION,
            "arguments": arguments, "task_id": task_id, "run_id": run_id})
        dispatch = authority_digest("SOLVIO_TASK_STEP_DISPATCH_V1", {"effect": effect,
            "step_id": previous_id, "seq": actual_step["seq"], "attempt": actual_step["attempt"]})
        return (actual_step["dispatch_claimed_at"] is not None
                and actual_step["dispatch_binding_digest"] == dispatch == prior["dispatch_binding"])
    except (ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
        return False


def _native_receipt(receipt):
    _keys(receipt, {"native_id", "observed"}, "receipt")
    _id(receipt["native_id"], "native_id")
    if type(receipt["observed"]) is not dict or type(receipt["observed"].get("confirmed")) is not bool:
        raise ValueError("invalid_action_observation")
    _canonical(receipt)


def _receipt_body(row, action, status, receipt, reason=""):
    if reason not in {"", "action_account_access_required"} or (reason and status == "completed"):
        raise ValueError("invalid_action_outcome_reason")
    return {**({"reason": reason} if reason else {}), "version": VERSION, **{k: row[k] for k in ("task_id", "run_id", "resource_id", "contract_digest", "grant_reference", "action_id", "step_id", "action_digest", "dispatch_binding")},
        "service": action["service"], "operation": action["operation"], "account": action["account"],
        "target": action["target"], "payload_digest": _hash("SOLVIO_ACTION_PAYLOAD_V1", action["payload"]),
        "read_only": (action["service"], action["operation"]) in READ_OPERATIONS,
        "status": status, "native": receipt}


def record_outcome(ledger, bound, action_id, step_id, *, status, receipt=None, reason=""):
    """Persist observed terminal/non-dispatch evidence; unknown never becomes retry."""
    if status not in {"completed", "unknown", "not_dispatched"}:
        raise ValueError("invalid_action_outcome")
    if status == "completed":
        _native_receipt(receipt)
    elif receipt is not None:
        raise ValueError("unexpected_action_receipt")
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _with_grant(connection, ledger, bound, active=False)
        row = connection.execute("SELECT * FROM agent_action_claims WHERE task_id=? AND action_id=?", (bound.task_id, action_id)).fetchone()
        if row is None or row["step_id"] != step_id or row["run_id"] != bound.run_id or row["grant_reference"] != current.grant_reference:
            raise ValueError("action_claim_missing")
        action = next(a for a in current.actions if a["action_id"] == action_id)
        if row["action_digest"] != _hash("SOLVIO_ACTION_V1", action) or row["contract_digest"] != bound.contract_digest or row["resource_id"] != bound.resource_id:
            raise ValueError("action_claim_changed")
        body = _receipt_body(row, action, status, receipt, reason)
        encoded, digest = _canonical(body), _hash("SOLVIO_ACTION_RECEIPT_V1", body)
        if row["status"] != "claimed":
            if (row["status"], row["receipt_json"], row["receipt_digest"]) != (status, encoded, digest):
                raise ValueError("action_outcome_already_bound")
            return dict(body, receipt_digest=digest)
        connection.execute("UPDATE agent_action_claims SET status=?,receipt_json=?,receipt_digest=?,updated_at=? "
            "WHERE task_id=? AND action_id=? AND status='claimed'", (status, encoded, digest, time.time(), bound.task_id, action_id))
        return dict(body, receipt_digest=digest)


def _read_receipt(connection, ledger, row):
    contract = connection.execute("SELECT * FROM agent_action_contracts WHERE run_id=?", (row["run_id"],)).fetchone()
    if contract is None:
        raise ValueError("action_contract_missing")
    bound = _with_grant(connection, ledger, _bound_row(contract), active=False)
    action = next((a for a in bound.actions if a["action_id"] == row["action_id"]), None)
    if action is None or row["action_digest"] != _hash("SOLVIO_ACTION_V1", action) or row["resource_id"] != bound.resource_id or row["contract_digest"] != bound.contract_digest or row["task_id"] != bound.task_id or row["grant_reference"] != bound.grant_reference:
        raise ValueError("action_receipt_binding_changed")
    body = json.loads(row["receipt_json"])
    if row["status"] == "completed":
        _native_receipt(body.get("native"))
    elif body.get("native") is not None:
        raise ValueError("invalid_action_unknown_receipt")
    if body != _receipt_body(row, action, row["status"], body.get("native"), body.get("reason", "")) or row["receipt_digest"] != _hash("SOLVIO_ACTION_RECEIPT_V1", body):
        raise ValueError("action_receipt_changed")
    from solvio.agent_runtime.action_account_rebinding import validate_native_receipt
    validate_native_receipt(connection, ledger, bound, action, body)
    if action["service"] == "gmail" and action["operation"] == "compose_draft" and body["status"] == "completed":
        from solvio.agent_runtime.action_draft_composition import validate_native_receipt as validate_composition
        validate_composition(connection, ledger, bound, action, body)
    step = connection.execute("SELECT * FROM agent_steps WHERE step_id=?", (row["step_id"],)).fetchone()
    if step is None or step["run_id"] != row["run_id"] or step["dispatch_binding_digest"] != row["dispatch_binding"]:
        raise ValueError("action_receipt_step_changed")
    if row["requirement_id"] and row["requirement_id"] != row["action_id"]:
        raise ValueError("action_receipt_requirement_changed")
    return dict(body, receipt_digest=row["receipt_digest"], requirement=row["requirement_id"], requirements_digest=row["requirements_digest"])


def read_receipts(ledger, run_id):
    """Every terminal attempt, including immutable non-dispatch history."""
    with ledger._open() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            f"SELECT {_CLAIM_COLUMNS} FROM agent_action_attempt_receipts WHERE run_id=? UNION ALL "
            f"SELECT {_CLAIM_COLUMNS} FROM agent_action_claims WHERE run_id=? AND status<>'claimed' "
            "ORDER BY created_at,action_id,step_id", (run_id, run_id)).fetchall()
        return [_read_receipt(connection, ledger, row) for row in rows]


def receipt_at(ledger, run_id, step_id):
    return next((r for r in read_receipts(ledger, run_id) if r["step_id"] == step_id), None)


def bind_requirement(ledger, run_id, action_id, step_id, requirement_id):
    if requirement_id != action_id:
        raise ValueError("action_requirement_identity_changed")
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM agent_action_claims WHERE run_id=? AND action_id=? AND step_id=?", (run_id, action_id, step_id)).fetchone()
        if row is None or row["status"] != "completed":
            raise ValueError("action_completed_receipt_required")
        receipt = _read_receipt(connection, ledger, row)
        task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        requirements = RQ.load(task["requirements"], objective=task["objective"])
        expected_kind = RQ.ASK if receipt["read_only"] else RQ.ACTION
        if requirements is None or requirement_id not in {r["id"] for r in requirements[expected_kind]}:
            raise ValueError("action_requirement_not_bound")
        digest = RQ.digest_of(requirements)
        if row["requirement_id"]:
            if (row["requirement_id"], row["requirements_digest"]) != (requirement_id, digest):
                raise ValueError("action_requirement_already_bound")
            return
        connection.execute("UPDATE agent_action_claims SET requirement_id=?,requirements_digest=? "
            "WHERE task_id=? AND action_id=? AND requirement_id=''", (requirement_id, digest, row["task_id"], action_id))


@dataclass(frozen=True)
class CompletionEvidence:
    evidence: str
    requirement: str


def completion_evidence(ledger, run_id):
    task = ledger.get_task(ledger.get_run(run_id).task_id) if ledger.get_run(run_id) else None
    if task is None:
        return ()
    bound = RQ.load(task.requirements, objective=task.objective)
    if bound is None:
        return ()
    digest = RQ.digest_of(bound)
    result = []
    for receipt in read_receipts(ledger, run_id):
        if (receipt["status"] != "completed"
                or not receipt["native"]["observed"]["confirmed"] or not receipt["requirement"]):
            continue
        kind = RQ.ASK if receipt["read_only"] else RQ.ACTION
        if receipt["requirements_digest"] != digest or receipt["requirement"] not in {r["id"] for r in bound[kind]}:
            raise ValueError("action_requirements_changed")
        result.append(CompletionEvidence(
            f"{receipt['service']}.{receipt['operation']} {receipt['action_id']} → {receipt['native']['native_id']} [{receipt['receipt_digest']}]",
            receipt["requirement"]))
    return tuple(result)
