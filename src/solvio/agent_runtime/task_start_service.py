"""Idempotente Annahme eines bereits authentifizierten Auftrags.

Die Quelle wird im vorhandenen Agentenbuch gebunden. Aufgabe, erster Lauf und
Quellenverweis entstehen atomar; der Lauf wird erst nach Grant und Kostenpolicy
fahrbar. Ein Prozessverlust dazwischen erzeugt weder Arbeit ohne Befugnis noch
einen zweiten Auftrag. Der Freigabeentscheid bleibt im Approval-Journal.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import time

from solvio.logging_setup import get_logger

from solvio.agent_runtime import authority, store as S
from solvio.agent_runtime import document_contract as DC
from solvio.agent_runtime import file_inputs as FI
from solvio.agent_runtime import action_contract as AC
from solvio.agent_runtime import action_intent as AI
from solvio.agent_runtime import task_revisions as TR
from solvio.agent_runtime.costs import CostLedger
from solvio.agent_runtime.task_authority import (
    CapabilityGrant, TaskAuthority, VerifiedTaskReceipt,
)

TASK_STARTS = frozenset({"agent_task_research", "agent_task_build", "agent_task_action", "agent_task_task"})
# N2: READ_ONLY beschreibt Wirkung, nicht Zusatzkosten. Nur die gemessene
# lokale Implementierung ist ohne eigene Kostenroute delegierbar:
# portal.list_portals liest BINDINGS und vault.has (lokale Datei/Keychain).
# Insbesondere research_quick, document_ask und bot_consult benutzen APIs.
# N3 nutzt native Recherche; N7 ergaenzt gebundene Dienst-/Kostenkontrakte.
# N8/C4 §4: result_files_list liest nur Deskriptoren der eigenen Ergebnisdateien
# (agent_runtime/result_files.py, free_local). memory_recall bleibt DEFER (E8).
# Stufe S1 (Kurskorrektur 25.09.2026): die lesenden Mail- und Kalenderhandler,
# verpackt in capabilities/task_read.py (Google-API lesend, keine Abrechnung).
def _private_task(connection, task_id):
    """A follow-up inherits what its task was started with (ADR-0040): the
    first source grant of the task names the private-data tools or it does not."""
    first = connection.execute("SELECT capabilities FROM agent_task_sources WHERE task_id=? "
                               "ORDER BY created_at, rowid LIMIT 1", (task_id,)).fetchone()
    if first is None:
        return False
    return any(item.get("name") in PRIVATE_DATA_CAPABILITIES for item in json.loads(first["capabilities"]))


LOCAL_CAPABILITIES = {
    "owner_task_overview": 1,
    "portal_list": 1, "result_files_list": 1,
    "gmail_list_recent": 1, "gmail_search": 1, "gmail_read_message": 1, "gmail_read_thread": 1,
    "calendar_list_events": 1, "calendar_get_event": 1, "calendar_search_events": 1,
    "calendar_find_availability": 1,
}
# Only granted to a chat task started with `private_data` (ADR-0040).
PRIVATE_DATA_CAPABILITIES = frozenset({
    "owner_task_overview",
    "gmail_list_recent", "gmail_search", "gmail_read_message", "gmail_read_thread",
    "calendar_list_events", "calendar_get_event", "calendar_search_events", "calendar_find_availability"})
log = get_logger("agent_runtime")
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_task_sources (
    principal TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
    run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id),
    receipt_method TEXT NOT NULL,
    receipt_reference TEXT NOT NULL UNIQUE,
    authorizer TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    cost_threshold_cents INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('preparing','ready')),
    created_at REAL NOT NULL,
    action_intent_digest TEXT NOT NULL DEFAULT '',
    task_revision_digest TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(principal,request_id)
);
"""


def _migrate_sources(connection):
    """Remove only task_id uniqueness, preserving rows and all other keys.

    Called under BEGIN IMMEDIATE. No FK is disabled, no row is rewritten and
    no caller-defined column/index/trigger is silently discarded. This table
    currently has no incoming foreign keys; an unknown referencing schema is
    refused before DROP rather than cascading into another journal.
    """
    table = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_task_sources'").fetchone()
    if table is None:
        connection.execute(SCHEMA)
    else:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(agent_task_sources)")}
        expected = {"principal", "request_id", "request_digest", "task_id", "run_id", "receipt_method",
            "receipt_reference", "authorizer", "capabilities", "cost_threshold_cents", "state", "created_at",
            "action_intent_digest", "task_revision_digest"}
        if columns - expected or expected - columns - {"action_intent_digest", "task_revision_digest"}:
            raise ValueError("task_source_schema_changed")
        for name in ("action_intent_digest", "task_revision_digest"):
            if name not in columns:
                connection.execute("ALTER TABLE agent_task_sources ADD COLUMN " + name + " TEXT NOT NULL DEFAULT ''")
        indices = connection.execute("PRAGMA index_list(agent_task_sources)").fetchall()
        task_unique = {row[1] for row in indices if row[2] and
            [r[2] for r in connection.execute("SELECT * FROM pragma_index_info(?)", (row[1],))] == ["task_id"]}
        if task_unique:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if any(r[2] == "agent_task_sources" for t in tables
                    for r in connection.execute("SELECT * FROM pragma_foreign_key_list(?)", (t[0],))):
                raise ValueError("task_source_referencing_schema_requires_migration")
            extras = [row["sql"] for row in connection.execute("SELECT name,sql FROM sqlite_master "
                "WHERE tbl_name='agent_task_sources' AND type IN ('index','trigger') AND sql IS NOT NULL")
                if row["name"] not in task_unique]
            connection.execute(SCHEMA.replace("agent_task_sources", "agent_task_sources_revision_migration"))
            fields = ",".join(sorted(expected))
            connection.execute("INSERT INTO agent_task_sources_revision_migration (" + fields + ") SELECT " + fields + " FROM agent_task_sources")
            original = connection.execute("SELECT " + fields + " FROM agent_task_sources ORDER BY principal,request_id").fetchall()
            copied = connection.execute("SELECT " + fields + " FROM agent_task_sources_revision_migration ORDER BY principal,request_id").fetchall()
            if [tuple(row) for row in original] != [tuple(row) for row in copied]:
                raise ValueError("task_source_migration_changed_rows")
            connection.execute("DROP TABLE agent_task_sources")
            connection.execute("ALTER TABLE agent_task_sources_revision_migration RENAME TO agent_task_sources")
            for sql in extras:
                connection.execute(sql)
    connection.execute("CREATE INDEX IF NOT EXISTS agent_task_sources_task ON agent_task_sources(task_id)")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("task_source_migration_foreign_keys")


def request_identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", value):
        raise ValueError("invalid_client_request_id")
    return value


def conversation_reference(value) -> str:
    """Ein Chatverweis: leer, oder genau die vom Core gepraegte Form `c-` + 16 hex."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"c-[0-9a-f]{16}", value):
        raise ValueError("invalid_conversation_ref")
    return value


def _digest(data: dict) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(b"SOLVIO_TASK_START_V1\0" + raw).hexdigest()


@dataclass(frozen=True)
class AuthorizedTaskStart:
    """Core-only Ergebnis einer verifizierten Sitzung, kein HTTP-Argument."""

    receipt: VerifiedTaskReceipt
    request_id: str
    capability: str
    arguments_digest: str
    # Verified HTTP input only; never part of model/Capability arguments.
    document_request: DC.DocumentRequest | None = None
    # Only the closed, handshake-derived browser voice guard can occupy this
    # field. It is transient; a successfully admitted TaskGrant outlives voice.
    browser_voice_authorization: object | None = None
    app_voice_authorization: object | None = None
    action_request: AC.ActionRequest | None = None
    action_intent: AI.IntentRequest | None = None
    portal_admission: object | None = None
    file_request: FI.FileTaskRequest | None = None
    # N8/C3: der Chat, an den ein aus Dashboard/App gestarteter Auftrag gebunden
    # wird. Leer heisst: kein Chat — und dann ist der Digest byteidentisch zu vorher.
    conversation_ref: str = ""
    # Kurskorrektur S1 / ADR-0040: ein Auftrag aus dem Chat, der Postfach oder
    # Kalender des Owners lesen soll. Nur dann bekommt er die Lesewerkzeuge — und
    # seine Sitzung keinen Webzugriff. False laesst den Digest bytegleich.
    private_data: bool = False

    async def dispatch_current(self) -> bool:
        from solvio.browser_voice_session import BrowserVoiceTaskAuthorization
        from solvio.voice_task_session import AppVoiceTaskAuthorization
        browser = self.browser_voice_authorization
        app = self.app_voice_authorization
        if self.receipt.reference.startswith("browser-voice:"):
            return (app is None and type(browser) is BrowserVoiceTaskAuthorization
                    and await browser.current(self))
        if self.receipt.reference.startswith("app-voice:"):
            return (browser is None and type(app) is AppVoiceTaskAuthorization
                    and await app.current(self))
        return browser is None and app is None

    @classmethod
    def bind(cls, *, receipt, request_id, capability, arguments, document_request=None,
             action_request=None, action_intent=None, portal_admission=None, file_request=None,
             conversation_ref="", private_data=False):
        if type(receipt) is not VerifiedTaskReceipt or capability not in TASK_STARTS:
            raise ValueError("invalid_task_start_receipt")
        if type(private_data) is not bool:
            raise ValueError("invalid_task_start_receipt")
        request_identifier(request_id)
        conversation_ref = conversation_reference(conversation_ref)
        document = DC.validate_request(document_request) if document_request is not None else None
        files = FI.validate_request(file_request) if file_request is not None else None
        if files is not None and (document is not None or capability != "agent_task_research"):
            raise ValueError("exclusive_files_research_required")
        if document is not None and capability != "agent_task_research":
            raise ValueError("document_requires_research")
        action = AC.validate_request(action_request) if action_request is not None else None
        intent = AI.validate_request(action_intent) if action_intent is not None else None
        if (action is not None and intent is not None) or ((capability == "agent_task_action") != (action is not None or intent is not None)):
            raise ValueError("action_contract_required")
        binding = arguments if document is None else {
            "arguments": arguments, "document_request": document.descriptor}
        if action is not None:
            binding = {"arguments": arguments, "action_request": action.descriptor}
        if intent is not None:
            binding = {"arguments": arguments, "action_intent": intent.descriptor}
        if files is not None:
            binding = {"arguments": arguments, "file_request": files.descriptor}
        # Nur wenn ein Chat gebunden wird, aendert sich der Digest — ein leerer
        # Verweis laesst den bisherigen Pfad und damit alte Digests bytegleich.
        if conversation_ref:
            binding = {"start": binding, "conversation_ref": conversation_ref}
        if private_data:
            if capability != "agent_task_task" or not conversation_ref or files is not None or document is not None:
                raise ValueError("private_data_requires_chat_task")
            binding = {"start": binding, "private_data": True}
        return cls(receipt, request_id, capability, _digest(binding), document,
                   action_request=action, action_intent=intent, portal_admission=portal_admission,
                   file_request=files, conversation_ref=conversation_ref, private_data=private_data)

    def matches(self, capability, arguments, principal, origin) -> bool:
        document = self.document_request
        files = self.file_request
        if files is not None:
            if (type(files) is not FI.FileTaskRequest or document is not None
                    or capability != "agent_task_research"):
                return False
            try:
                files.__post_init__()
            except ValueError:
                return False
        if document is not None:
            if type(document) is not DC.DocumentRequest or capability != "agent_task_research":
                return False
            try:
                document.__post_init__()
            except ValueError:
                return False
        binding = arguments if document is None else {
            "arguments": arguments, "document_request": document.descriptor}
        action = self.action_request
        intent = self.action_intent
        if (action is not None and intent is not None) or ((capability == "agent_task_action") != (action is not None or intent is not None)):
            return False
        if intent is not None:
            if type(intent) is not AI.IntentRequest:
                return False
            try:
                AI.validate_request(intent)
            except ValueError:
                return False
            binding = {"arguments": arguments, "action_intent": intent.descriptor}
        if action is not None:
            try:
                if type(action) is not AC.ActionRequest:
                    return False
                AC.validate_request(action)
            except ValueError:
                return False
            binding = {"arguments": arguments, "action_request": action.descriptor}
        if files is not None:
            binding = {"arguments": arguments, "file_request": files.descriptor}
        if self.conversation_ref:
            try:
                conversation_reference(self.conversation_ref)
            except ValueError:
                return False
            binding = {"start": binding, "conversation_ref": self.conversation_ref}
        if self.private_data is not False:
            if (self.private_data is not True or capability != "agent_task_task"
                    or not self.conversation_ref or files is not None or document is not None):
                return False
            binding = {"start": binding, "private_data": True}
        expected = {"app_session": "trusted_interactive_app",
                    "dashboard_session": "trusted_dashboard"}
        return (type(self.receipt) is VerifiedTaskReceipt
                and self.receipt.method in expected
                and expected[self.receipt.method] == getattr(origin, "value", origin)
                and principal == self.receipt.authorizer
                and capability == self.capability
                and capability in TASK_STARTS
                and self.arguments_digest == _digest(binding))


@dataclass(frozen=True)
class TaskStepAuthority:
    reference: str
    task_id: str
    run_id: str
    step_id: str


class TaskStartService:
    def __init__(self, ledger, *, grants: TaskAuthority, costs: CostLedger, router=None):
        self.ledger, self.grants, self.costs, self.router = ledger, grants, costs, router
        with ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _migrate_sources(connection)
        TR.initialize(ledger)
        with ledger._open() as connection:
            connection.executescript(AC.SCHEMA)
            connection.executescript(AI.SCHEMA)
            from solvio.agent_runtime.action_account_rebinding import SCHEMA as ACCOUNT_REBINDINGS
            connection.executescript(ACCOUNT_REBINDINGS)
            from solvio.agent_runtime.action_draft_composition import SCHEMA as ACTION_COMPOSITIONS
            connection.executescript(ACTION_COMPOSITIONS)
            from solvio.agent_runtime.portal_connection import SCHEMA as PORTAL_CONNECTIONS
            connection.executescript(PORTAL_CONNECTIONS)

    def capabilities_for(self, scope: str, *, private_data: bool = False) -> tuple[CapabilityGrant, ...]:
        # Recherche/Bauen sind die bereits vorhandenen Auftragsarten. Ihre
        # generischen Capability-Schritte lesen; Builder schreiben nur in der
        # bestehenden isolierten Workspace-Naht. Alltagswirkungen werden in N7
        # als explizite Ressourcenkontrakte hinzugefuegt, nie aus Modelltext.
        entries = []
        for name in self.router.names() if self.router is not None else ():
            spec = self.router.spec(name)
            if (spec and spec.is_read_only() and not authority.is_blocked(name)
                    and LOCAL_CAPABILITIES.get(name) == spec.version
                    and (private_data is True and scope == S.SCOPE_TASK
                         or name not in PRIVATE_DATA_CAPABILITIES)):
                entries.append(CapabilityGrant(name, spec.version))
        if scope in {S.SCOPE_RESEARCH, S.SCOPE_TASK}:
            # A local output stays in this run's result store. Unlike router
            # actions this narrow grant is consumed only by the bound file
            # specialist, with explicit original requirement IDs and evidence.
            from solvio.agent_runtime.artifact_creation import capability_grant
            entries.append(capability_grant())
        return tuple(entries)

    def create(self, *, objective, scope, origin, principal, receipt,
               request_id, target_repo="", conversation_ref="", predecessor_ref="",
               budget=None, document_request=None, action_request=None, action_intent=None, portal_admission=None,
               file_request=None, private_data=False):
        if type(receipt) is not VerifiedTaskReceipt or receipt.authorizer != principal:
            raise ValueError("verified_task_authorizer_required")
        if type(private_data) is not bool or private_data and (
                scope != S.SCOPE_TASK or not conversation_ref
                or document_request is not None or file_request is not None):
            raise ValueError("private_data_requires_chat_task")
        request_identifier(request_id)
        S._require(S.SCOPES, scope, "scope")
        if scope == S.SCOPE_TASK and (target_repo or (receipt.method, origin) not in {
                ("app_session", "trusted_interactive_app"),
                ("dashboard_session", "trusted_dashboard")}):
            raise ValueError("native_task_requires_authenticated_request")
        if action_request is not None and action_intent is not None:
            raise ValueError("action_contract_required")
        if action_intent is not None:
            if (type(action_intent) is not AI.IntentRequest or target_repo
                    or (receipt.method, origin) not in {("app_session", "trusted_interactive_app"),
                                                       ("dashboard_session", "trusted_dashboard")}):
                raise ValueError("action_requires_authenticated_request")
            AI.validate_request(action_intent)
        if (scope == S.SCOPE_ACTION) != (action_request is not None or action_intent is not None):
            raise ValueError("action_contract_required")
        if action_request is not None:
            if (type(action_request) is not AC.ActionRequest or target_repo
                    or (receipt.method, origin) not in {
                        ("app_session", "trusted_interactive_app"),
                        ("dashboard_session", "trusted_dashboard")}):
                raise ValueError("action_requires_authenticated_request")
            AC.validate_request(action_request)
        if document_request is not None:
            if (type(document_request) is not DC.DocumentRequest or scope != S.SCOPE_RESEARCH
                    or target_repo or (receipt.method, origin) not in {
                        ("app_session", "trusted_interactive_app"),
                        ("dashboard_session", "trusted_dashboard")}):
                raise ValueError("document_requires_authenticated_research")
            document_request.__post_init__()
        if file_request is not None:
            if (type(file_request) is not FI.FileTaskRequest or document_request is not None
                    or scope != S.SCOPE_RESEARCH or target_repo
                    or (receipt.method, origin) not in {
                        ("app_session", "trusted_interactive_app"),
                        ("dashboard_session", "trusted_dashboard")}):
                raise ValueError("files_require_authenticated_research")
            file_request.__post_init__()
        safe = S._safe_text(objective, S.MAX_OBJECTIVE, where="agent_task.objective")
        if safe != objective:
            raise ValueError("task_objective_would_change")
        request_binding = {"objective": objective, "scope": scope, "origin": origin,
            "principal": principal, "target_repo": target_repo,
            "conversation_ref": conversation_ref, "predecessor_ref": predecessor_ref}
        if private_data:
            request_binding["private_data"] = True
        if document_request is not None:
            request_binding["document_request"] = document_request.descriptor
        if file_request is not None:
            request_binding["file_request"] = file_request.descriptor
        if action_request is not None:
            request_binding["action_request"] = action_request.descriptor
        if action_intent is not None:
            request_binding["action_intent"] = action_intent.descriptor
        binding = _digest(request_binding)
        threshold = self.costs.settings()["ask_threshold_cents"]
        now = time.time()
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT * FROM agent_task_sources WHERE principal=? "
                "AND request_id=?", (principal, request_id)).fetchone()
            if prior:
                if prior["request_digest"] != binding:
                    raise ValueError("task_request_binding_changed")
                task_id, run_id = prior["task_id"], prior["run_id"]
            else:
                task_id, run_id = S.new_task_id(), S.new_run_id()
                document = (DC.prepare(document_request, task_id=task_id, run_id=run_id)
                            if document_request is not None else None)
                files = (FI.prepare(file_request, task_id=task_id, run_id=run_id)
                         if file_request is not None else None)
                actions = (AC.prepare(action_request, task_id=task_id, run_id=run_id)
                           if action_request is not None else None)
                entries = self.capabilities_for(scope, private_data=private_data)
                if document is not None:
                    # The closed upload contract retains its original grant.
                    # Generic research-result production is a separate route.
                    from solvio.agent_runtime.artifact_creation import CAPABILITY as ARTIFACT_CAPABILITY
                    entries = tuple(entry for entry in entries
                                    if entry.name != ARTIFACT_CAPABILITY) + (document.capability_grant,)
                if files is not None:
                    entries = (files.capability_grant,)
                if actions is not None:
                    entries = (actions.capability_grant,)
                capabilities = json.dumps([{"name": c.name, "version": c.version,
                    "constraints": c.constraints} for c in entries], sort_keys=True)
                connection.execute("INSERT INTO agent_tasks (task_id,objective,scope,target_repo,"
                    "created_at,created_origin,created_principal,conversation_ref,predecessor_ref,"
                    "state,budget,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, objective, scope, target_repo, now, origin, principal,
                     conversation_ref, predecessor_ref, S.TASK_ACTIVE, json.dumps(budget or {}), now))
                connection.execute("INSERT INTO agent_runs (run_id,task_id,state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?)", (run_id, task_id, S.CREATED, now, now))
                if document is not None:
                    DC.record_prepared(connection, document, now=now)
                if files is not None:
                    FI.record_prepared(connection, files, now=now)
                if actions is not None:
                    AC.record_prepared(connection, actions, now=now)
                    from solvio.agent_runtime.portal_connection import record_admitted
                    record_admitted(connection, actions, principal, portal_admission, now)
                connection.execute("INSERT INTO agent_task_sources(principal,request_id,request_digest,task_id,run_id,receipt_method,receipt_reference,authorizer,capabilities,cost_threshold_cents,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (principal, request_id, binding, task_id, run_id, receipt.method,
                     receipt.reference, receipt.authorizer, capabilities, threshold, "preparing", now))
                if action_intent is not None:
                    AI.admit(connection, task_id=task_id, run_id=run_id, now=now)
        try:
            self.finish(run_id)
        except Exception as exc:
            # Der Commit IST die Annahme. Eine noch fehlende Initialisierung
            # darf danach nicht als 'nichts angelegt' beantwortet werden.
            log.warning("agent_runtime.task_initialization_pending", run_id=run_id,
                        kind=type(exc).__name__)
        return self.ledger.get_task(task_id), self.ledger.get_run(run_id)

    def admit_followup(self, prepared: TR.PreparedFollowup):
        """One new source/run in the same task; finish is crash-recoverable.

        Only a verified direct Owner follow-up can reach this internal seam.
        It is not exposed as a model capability. No executable grantless run is committed:
        source=preparing and the revision are present before the old task is
        reopened. Costs remain the existing cumulative task policy.
        """
        if type(prepared) is not TR.PreparedFollowup or prepared.ledger_path != os.path.realpath(self.ledger.path):
            raise ValueError("followup_preparation_required")
        request = TR.canonical_followup(prepared.request)
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task, parent = TR._task_run(connection, request["run_id"])
            TR._receipt(prepared.receipt, task)
            chain = TR._chain(connection, task)
            prior = TR._prior(connection, task, request, prepared.receipt, chain)
            if prior is not None:
                task_id, run_id = prior["task_id"], prior["run_id"]
                source = connection.execute("SELECT * FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone()
                if source is None:
                    raise ValueError("task_revision_source_missing")
                original_receipt = VerifiedTaskReceipt(source["receipt_method"], source["receipt_reference"], source["authorizer"])
                TR.validate_source(connection, task_id, run_id, original_receipt, json.loads(source["capabilities"]))
            else:
                if connection.execute("SELECT 1 FROM agent_task_sources WHERE principal=? AND request_id=?",
                        (prepared.receipt.authorizer, request["client_request_id"])).fetchone():
                    raise ValueError("followup_request_conflict")
                files_request = TR.prepared_file_request(prepared)
                task_id, run_id, now = task["task_id"], S.new_run_id(), time.time()
                connection.execute("INSERT INTO agent_runs(run_id,task_id,parent_run_id,state,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?)", (run_id, task_id, parent["run_id"], S.CREATED, now, now))
                revision = TR.record_admitted(connection, self.ledger, prepared, run_id=run_id)
                private = _private_task(connection, task_id)
                entries = self.capabilities_for(task["scope"], private_data=private)
                if private and not any(entry.name in PRIVATE_DATA_CAPABILITIES for entry in entries):
                    # ADR-0040 (Review S1R2-3): without its mail/calendar tools the follow-up
                    # would continue the same native thread WITH web search. Refuse instead.
                    raise ValueError("private_task_tools_unavailable")
                if files_request is not None:
                    files = FI.prepare(files_request, task_id=task_id, run_id=run_id)
                    FI.record_prepared(connection, files, now=now)
                    entries = (files.capability_grant,)
                allowed = sorted([{"name": entry.name, "version": entry.version,
                    "constraints": entry.constraints} for entry in entries], key=lambda entry: entry["name"])
                descriptor = TR.source_descriptor(connection, task_id, run_id, prepared.receipt, allowed)
                connection.execute("INSERT INTO agent_task_sources(principal,request_id,request_digest,task_id,run_id,"
                    "receipt_method,receipt_reference,authorizer,capabilities,cost_threshold_cents,state,created_at,task_revision_digest) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (prepared.receipt.authorizer, request["client_request_id"],
                    _digest(descriptor), task_id, run_id, prepared.receipt.method, prepared.receipt.reference,
                    prepared.receipt.authorizer, json.dumps(allowed, sort_keys=True),
                    descriptor["cost_threshold_cents"], "preparing", now, revision["digest"]))
                TR.validate_source(connection, task_id, run_id, prepared.receipt, allowed)
                # record_admitted rechecked the exact eligible terminal parent
                # under this transaction. Reopen only that observed task state;
                # the parent's FAILED/SUCCEEDED result is never changed.
                if connection.execute("UPDATE agent_tasks SET state=?,updated_at=? WHERE task_id=? AND state=?",
                        (S.TASK_ACTIVE, now, task_id, task["state"])).rowcount != 1:
                    raise ValueError("followup_task_state_changed")
        try:
            self.finish(run_id)
        except Exception as exc:
            # Accepted is durable even when grant initialization needs recovery.
            log.warning("agent_runtime.task_initialization_pending", run_id=run_id, kind=type(exc).__name__)
        return self.ledger.get_task(task_id), self.ledger.get_run(run_id)

    def finish(self, run_id: str) -> bool:
        with self.ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            if not self.ready(run_id):
                raise ValueError("task_revision_source_missing")
            return True
        if row["state"] == "ready":
            return True
        self.costs.configure(row["task_id"], ask_threshold_cents=row["cost_threshold_cents"])
        if not AI.check_resolution(self.ledger, run_id):
            return False
        entries = tuple(CapabilityGrant(**c) for c in json.loads(row["capabilities"]))
        DC.check_prepared(self.ledger, task_id=row["task_id"], run_id=run_id, entries=entries)
        FI.check_prepared(self.ledger, task_id=row["task_id"], run_id=run_id, entries=entries)
        AC.check_prepared(self.ledger, task_id=row["task_id"], run_id=run_id, entries=entries)
        receipt = VerifiedTaskReceipt(row["receipt_method"], row["receipt_reference"], row["authorizer"])
        with self.ledger._open() as connection:
            TR.validate_source(connection, row["task_id"], run_id, receipt, json.loads(row["capabilities"]))
        self.costs.configure(row["task_id"], ask_threshold_cents=row["cost_threshold_cents"])
        self.grants.issue(row["task_id"], run_id, receipt=receipt,
            capabilities=entries)
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            DC.check_prepared(self.ledger, task_id=row["task_id"], run_id=run_id, entries=entries)
            FI.check_prepared(self.ledger, task_id=row["task_id"], run_id=run_id, entries=entries)
            AC.check_prepared(self.ledger, task_id=row["task_id"], run_id=run_id, entries=entries)
            if not AI.check_resolution(self.ledger, run_id):
                raise ValueError("action_intent_unresolved")
            TR.validate_source(connection, row["task_id"], run_id, receipt, json.loads(row["capabilities"]))
            connection.execute("UPDATE agent_task_sources SET state='ready' WHERE run_id=? "
                               "AND state='preparing'", (run_id,))
        return True

    def ready(self, run_id: str) -> bool:
        with self.ledger._open() as connection:
            row = connection.execute("SELECT state FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                # A missing source of a managed child is not a legacy task.
                run = connection.execute("SELECT task_id,parent_run_id FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
                if run is not None and run["parent_run_id"] and connection.execute(
                        "SELECT 1 FROM agent_task_sources WHERE task_id=?", (run["task_id"],)).fetchone():
                    return False
                if connection.execute("SELECT 1 FROM agent_task_revisions WHERE run_id=?", (run_id,)).fetchone():
                    return False
                return True
            return row["state"] == "ready"
