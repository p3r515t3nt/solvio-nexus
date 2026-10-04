"""Unveraenderliche Owner-Folgeanweisungen im bestehenden Aufgabenledger.

Dies ist KEIN Eingang und keine Ausfuehrungsbefugnis. ``prepare`` liest nur.
``record_admitted`` schreibt nur innerhalb einer bereits gesperrten
Aufrufertransaktion und verlangt deren schon vorbereiteten neuen Run. Der
integrierende Eingang muss Run, Quelle, Grant/Initialisierung und Revision
gemeinsam festschreiben; dieses Modul erstellt/aktiviert keinen Run, erteilt
keinen Grant und committet niemals die Annahme. Ohne diese Integration darf
kein oeffentlicher Eingang den Baustein anbieten.

Kosten behalten task_id. Alte Runs, Originalziel und Originalanforderungen
werden nie umgeschrieben. Eingaben sind eigene, begrenzte Byte-Snapshots aus
read_result; ein Modellpfad ist keine Eingabe. Historische Reads brauchen
keine aktuelle Sitzung, authentifizierende HTTP-Aufrufer pruefen den Owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import re
import sqlite3
import time

from solvio.agent_runtime import store as S, requirements as RQ, result_files as F
from solvio.agent_runtime.task_authority import VerifiedTaskReceipt, _task_fingerprint, _grant_binding
from solvio.capabilities import task_read as TASK_READ

MAX_INPUTS = 4
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_REVISIONS = 100
MAX_TEXT = 2000
_RUN = re.compile(r"ar-[a-f0-9]{16}")
_ARTIFACT = re.compile(r"aa-[a-f0-9]{16}")
_HEX = re.compile(r"[a-f0-9]{64}")
# Read-only steps a follow-up may sit behind. The zero-cost tools of the native
# worker (portal_list, result_files_list — N8/C4 §4; mail/calendar reads — Stufe
# S1) are named ONCE in `_LOCAL_TOOLS`; every check below derives from it.
_LOCAL_TOOLS = frozenset({"portal_list", F.LIST_CAPABILITY}) | TASK_READ.PRIVATE_DATA_TOOLS
_READ_CAPABILITIES = _LOCAL_TOOLS | frozenset({"document_extract_text", "file_process"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_task_revisions (
 reference TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
 run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id),
 parent_run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id),
 revision INTEGER NOT NULL, principal TEXT NOT NULL, request_id TEXT NOT NULL,
 request_digest TEXT NOT NULL, receipt_method TEXT NOT NULL,
 receipt_reference TEXT NOT NULL UNIQUE, body_json TEXT NOT NULL,
 body_digest TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(task_id, revision), UNIQUE(principal, request_id)
);
CREATE TABLE IF NOT EXISTS agent_task_revision_inputs (
 revision_reference TEXT NOT NULL REFERENCES agent_task_revisions(reference),
 artifact_id TEXT NOT NULL, descriptor_json TEXT NOT NULL, content BLOB NOT NULL,
 PRIMARY KEY(revision_reference, artifact_id)
);
CREATE TABLE IF NOT EXISTS agent_task_revision_requirements (
 run_id TEXT PRIMARY KEY REFERENCES agent_task_revisions(run_id),
 payload TEXT NOT NULL, digest TEXT NOT NULL
);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(domain, value) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + _json(value).encode("utf-8")).hexdigest()


def initialize(ledger) -> None:
    """Bootstrapping only; reads below never migrate or create tables."""
    with ledger._open() as db:
        db.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)


def _has(db, table) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _run_id(value):
    if type(value) is not str or not _RUN.fullmatch(value):
        raise ValueError("invalid_followup_run_id")
    return value


def canonical_followup(value) -> dict:
    """Exact body for authenticated transport/receipt binding, never authority."""
    # The entrance will import this module; keep its existing validation lazy.
    from solvio.agent_runtime.task_start_service import request_identifier
    keys = {"run_id", "text", "expected_revision", "expected_digest", "input_artifact_ids", "client_request_id"}
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid_followup_fields")
    _run_id(value["run_id"])
    text = value["text"]
    if (type(text) is not str or text != text.strip() or not 1 <= len(text) <= MAX_TEXT
            or any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in text)):
        raise ValueError("invalid_followup_text")
    if S._safe_text(text, MAX_TEXT, where="task_revision.text") != text:
        raise ValueError("followup_text_would_change")
    revision = value["expected_revision"]
    if type(revision) is not int or not 1 <= revision < MAX_REVISIONS:
        raise ValueError("invalid_followup_revision")
    if type(value["expected_digest"]) is not str or not _HEX.fullmatch(value["expected_digest"]):
        raise ValueError("invalid_followup_digest")
    ids = value["input_artifact_ids"]
    if (type(ids) is not list or len(ids) > MAX_INPUTS or
            any(type(v) is not str or not _ARTIFACT.fullmatch(v) for v in ids) or len(set(ids)) != len(ids)):
        raise ValueError("invalid_followup_inputs")
    request_identifier(value["client_request_id"])
    # Order is part of the request: replay never silently sorts changed input.
    return json.loads(_json(value))


def followup_digest(value) -> str:
    return _digest("SOLVIO_TASK_FOLLOWUP_V1", canonical_followup(value))


@dataclass(frozen=True)
class RevisionInput:
    descriptor_json: str
    content: bytes = field(repr=False)

    @property
    def descriptor(self) -> dict:
        return json.loads(self.descriptor_json)


@dataclass(frozen=True)
class PreparedFollowup:
    ledger_path: str
    request_json: str
    task_id: str
    receipt: VerifiedTaskReceipt
    binding_json: str
    inputs: tuple[RevisionInput, ...] = field(repr=False)

    @property
    def request(self) -> dict:
        return json.loads(self.request_json)

    @property
    def request_digest(self) -> str:
        return followup_digest(self.request)

    @property
    def effective_objective(self) -> str:
        return json.loads(self.binding_json)["effective_objective"]


def _task_run(db, run_id):
    # Historical task/run identifiers retain the ledger's existing rules.
    # New follow-up wire bodies and new child ids are validated separately.
    run = db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (run["task_id"],)).fetchone() if run else None
    if task is None:
        raise ValueError("followup_run_not_found")
    return task, run


def _original(db, task, run):
    """Historical integrity, without treating an old receipt as new authority."""
    binding = {key: task[key] for key in ("task_id", "objective", "scope", "target_repo",
        "created_origin", "created_principal", "conversation_ref", "predecessor_ref", "requirements")}
    binding["base_run_id"] = run["run_id"]
    return binding


def _original_digest(db, task, run):
    return _digest("SOLVIO_TASK_REVISION_ORIGINAL_V1", _original(db, task, run))


def _requirements(db, run_id, objective):
    if not _has(db, "agent_task_revision_requirements"):
        return ""
    row = db.execute("SELECT * FROM agent_task_revision_requirements WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return ""
    if (_digest("SOLVIO_TASK_REVISION_REQUIREMENTS_V1", row["payload"]) != row["digest"]
            or RQ.load(row["payload"], objective=objective) is None):
        raise ValueError("task_revision_requirements_changed")
    return row["payload"]


def _chain(db, task):
    if not _has(db, "agent_task_revisions"):
        return []
    rows = db.execute("SELECT * FROM agent_task_revisions WHERE task_id=? ORDER BY revision", (task["task_id"],)).fetchall()
    out = []
    previous = None
    for row in rows:
        try:
            body = json.loads(row["body_json"])
            request = canonical_followup(body["request"])
            _, parent = _task_run(db, row["parent_run_id"])
            _, run = _task_run(db, row["run_id"])
            _, base = _task_run(db, body["base_run_id"])
            original = _original_digest(db, task, base)
            expected_parent = previous[0]["run_id"] if previous else base["run_id"]
            expected_digest = previous[0]["body_digest"] if previous else original
            expected_revision = previous[0]["revision"] + 1 if previous else 2
            valid = (row["body_json"] == _json(body) and row["body_digest"] == _digest("SOLVIO_TASK_REVISION_V1", body)
                and row["reference"] == "tr-" + row["body_digest"][:32]
                and body["original_digest"] == original
                and row["parent_run_id"] == expected_parent == request["run_id"]
                and request["expected_digest"] == expected_digest
                and request["expected_revision"] + 1 == row["revision"] == expected_revision
                and row["request_digest"] == followup_digest(request)
                and row["request_id"] == request["client_request_id"]
                and row["task_id"] == body["task_id"] == run["task_id"] == parent["task_id"]
                and run["parent_run_id"] == parent["run_id"] and row["run_id"] == body["run_id"]
                and row["principal"] == body["receipt"]["authorizer"] == task["created_principal"]
                and row["receipt_reference"] == body["receipt"]["reference"]
                and row["receipt_method"] == body["receipt"]["method"]
                and body["parent_result"] == _parent_result(parent))
            VerifiedTaskReceipt(**body["receipt"])
            texts = [entry[1]["request"]["text"] for entry in out] + [request["text"]]
            valid = valid and body["effective_objective"] == _objective(task["objective"], texts)
            if not valid:
                raise ValueError("task_revision_binding_changed")
            _read_inputs(db, row, body)
            _requirements(db, row["run_id"], body["effective_objective"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("task_revision_binding_changed") from exc
        out.append((row, body))
        previous = row, body
    return out


def _parent_result(run):
    return {**{key: run[key] for key in ("run_id", "task_id", "state", "finished_at", "outcome", "failure_category",
        "result_summary", "completion_verdict")},
        "plan_checkpoint_sha256": hashlib.sha256(run["plan_checkpoint"].encode("utf-8")).hexdigest()}


def _objective(original, texts):
    # No truncation or model rewrite: these are exact Owner instructions.
    result = original + "".join("\n\nFolgeanweisung " + str(index) + ":\n" + text for index, text in enumerate(texts, 2))
    if len(result) > S.MAX_OBJECTIVE:
        raise ValueError("followup_context_too_large")
    return result


def _view(row, body):
    return {"reference": row["reference"], "task_id": row["task_id"], "run_id": row["run_id"],
        "parent_run_id": row["parent_run_id"], "revision": row["revision"], "digest": row["body_digest"],
        "text": body["request"]["text"], "effective_objective": body["effective_objective"],
        "input_artifact_ids": list(body["request"]["input_artifact_ids"]),
        "receipt_reference": row["receipt_reference"], "created_at": row["created_at"]}


def _current(db, task, run, chain):
    for row, body in chain:
        if row["run_id"] == run["run_id"]:
            return _view(row, body)
    return {"reference": "", "task_id": task["task_id"], "run_id": run["run_id"],
        "parent_run_id": run["parent_run_id"], "revision": 1,
        "digest": _original_digest(db, task, run), "text": task["objective"],
        "effective_objective": task["objective"], "input_artifact_ids": [],
        "receipt_reference": "", "created_at": run["created_at"]}


def revision_for_run(ledger, run_id) -> dict:
    with ledger._open() as db:
        task, run = _task_run(db, run_id)
        return _current(db, task, run, _chain(db, task))


def history(ledger, run_id) -> list[dict]:
    with ledger._open() as db:
        task, run = _task_run(db, run_id)
        chain = _chain(db, task)
        if not chain:
            return [_current(db, task, run, chain)]
        _, base = _task_run(db, chain[0][1]["base_run_id"])
        return [_current(db, task, base, chain)] + [_view(row, body) for row, body in chain]


def effective_objective(ledger, run_id) -> str:
    return revision_for_run(ledger, run_id)["effective_objective"]


def owner_instruction_context(ledger, run_id) -> dict | None:
    """Verified Owner changes for model interpretation, never new authority.

    Do not recognize revision markers in arbitrary text. Only the persisted,
    authenticated revision chain can distinguish a later Owner instruction
    from quoted instructions in the original objective or previous results.
    """
    with ledger._open() as db:
        task, run = _task_run(db, run_id)
        chain = _chain(db, task)
        current = _current(db, task, run, chain)
        if current["revision"] == 1:
            return None
        return {"task_id": task["task_id"], "run_id": run_id,
            "revision": current["revision"], "digest": current["digest"],
            "effective_objective": current["effective_objective"],
            "urspruenglicher_auftrag": task["objective"],
            "folgeanweisungen": [{"revision": row["revision"], "text": body["request"]["text"]}
                for row, body in chain if row["revision"] <= current["revision"]]}


def requirements_for_run(ledger, run_id) -> str:
    with ledger._open() as db:
        task, run = _task_run(db, run_id)
        chain = _chain(db, task)
        view = _current(db, task, run, chain)
        return task["requirements"] if view["revision"] == 1 else _requirements(db, run_id, view["effective_objective"])


def task_view(ledger, run_id):
    """Read-only planning/result projection; never an authority task row."""
    with ledger._open() as db:
        task, run = _task_run(db, run_id)
        view = _current(db, task, run, _chain(db, task))
        requirements = task["requirements"] if view["revision"] == 1 else _requirements(db, run_id, view["effective_objective"])
        return replace(S._to_task(task), objective=view["effective_objective"], requirements=requirements)


def parent_result_snapshot(ledger, run_id) -> dict | None:
    """Validated prior result and its exact bytes from one SQLite snapshot.

    Context only, never a new result receipt. The explicit read transaction
    prevents a writer between chain validation and the parent-row projection
    from replacing the checkpoint that the revision actually bound.
    """
    with ledger._open() as db:
        db.execute("BEGIN")
        task, _ = _task_run(db, run_id)
        match = next(((row, body) for row, body in _chain(db, task) if row["run_id"] == run_id), None)
        if match is None:
            return None
        _, parent = _task_run(db, match[0]["parent_run_id"])
        return {"parent_run_id": parent["run_id"], "task_id": parent["task_id"],
            "result_summary": parent["result_summary"], "plan_checkpoint": parent["plan_checkpoint"]}


def _read_inputs(db, row, body):
    records = db.execute("SELECT artifact_id,descriptor_json,content FROM agent_task_revision_inputs WHERE revision_reference=?",
                         (row["reference"],)).fetchall()
    by_id = {r["artifact_id"]: r for r in records}
    if set(by_id) != set(body["request"]["input_artifact_ids"]) or len(body["inputs"]) != len(by_id):
        raise ValueError("task_revision_input_changed")
    out = []
    for descriptor in body["inputs"]:
        item = by_id.get(descriptor["id"])
        if (item is None or item["descriptor_json"] != _json(descriptor) or type(item["content"]) is not bytes
                or len(item["content"]) != descriptor["size"]
                or hashlib.sha256(item["content"]).hexdigest() != descriptor["sha256"]):
            raise ValueError("task_revision_input_changed")
        out.append(RevisionInput(item["descriptor_json"], item["content"]))
    if [item.descriptor["id"] for item in out] != body["request"]["input_artifact_ids"] or sum(len(i.content) for i in out) > MAX_INPUT_BYTES:
        raise ValueError("task_revision_input_changed")
    return tuple(out)


def read_inputs(ledger, run_id) -> tuple[RevisionInput, ...]:
    with ledger._open() as db:
        task, _ = _task_run(db, run_id)
        for row, body in _chain(db, task):
            if row["run_id"] == run_id:
                return _read_inputs(db, row, body)
        return ()


def _receipt(receipt, task):
    if (type(receipt) is not VerifiedTaskReceipt or receipt.method not in {"app_session", "dashboard_session"}
            or receipt.authorizer != task["created_principal"]):
        raise ValueError("followup_owner_receipt_required")


def _prior(db, task, request, receipt, chain):
    if not _has(db, "agent_task_revisions"):
        return None
    row = db.execute("SELECT * FROM agent_task_revisions WHERE principal=? AND request_id=?",
                     (receipt.authorizer, request["client_request_id"])).fetchone()
    if row is None:
        return None
    if row["task_id"] != task["task_id"] or row["request_digest"] != followup_digest(request):
        raise ValueError("followup_request_conflict")
    return next((_view(r, b) for r, b in chain if r["reference"] == row["reference"]), None)


def replay(ledger, value, receipt) -> dict | None:
    request = canonical_followup(value)
    with ledger._open() as db:
        task, _ = _task_run(db, request["run_id"])
        _receipt(receipt, task)
        return _prior(db, task, request, receipt, _chain(db, task))


def _failed_research_readonly(db, task, parent, chain, *, prepared_run_id=""):
    """A new Owner instruction may follow a negative information result.

    This does not resume or relabel the failed run. The narrow allowance must
    not reopen file generation, development or an unresolved external effect
    merely because their final assessment also used ``goal_unverified``.
    Requirements alone are not evidence of read-only execution: inspect the
    actual run/step, grant and artifact history as well.
    """
    from solvio.agent_runtime import specialists as SP
    reason = "followup_failed_research_not_readonly"
    objectives = [(task["requirements"], task["objective"])] + [
        (_requirements(db, row["run_id"], body["effective_objective"]), body["effective_objective"])
        for row, body in chain]
    for payload, objective in objectives:
        bound = RQ.load(payload, objective=objective)
        if bound is None or not bound[RQ.ASK] or bound[RQ.ACTION] or bound[RQ.UNCLEAR]:
            raise ValueError(reason)
    for run in db.execute("SELECT * FROM agent_runs WHERE task_id=?", (task["task_id"],)):
        if run["run_id"] == prepared_run_id:
            continue
        if (any(run[key] for key in ("development_ref", "workspace_path", "workspace_repo",
                "workspace_branch", "workspace_base", "branch_ref", "boundary"))
                or run["state"] == S.FAILED and run["failure_category"] != "goal_unverified"):
            raise ValueError(reason)
        if _has(db, "agent_extension_selection") and db.execute(
                "SELECT 1 FROM agent_extension_selection WHERE run_id=?", (run["run_id"],)).fetchone():
            raise ValueError(reason)
    for grant in db.execute("SELECT capabilities FROM agent_task_grants WHERE task_id=?", (task["task_id"],)):
        # Local result permission is part of new research admissions. An unused
        # exact grant is harmless here; actual file work is still excluded by
        # requirements, artifacts and the executed-step checks below.
        if any(entry["name"] not in _LOCAL_TOOLS | {"document_extract_text"} and entry != _artifact_grant()
               for entry in json.loads(grant["capabilities"])):
            raise ValueError(reason)
    if db.execute("SELECT 1 FROM agent_artifacts a JOIN agent_runs r USING(run_id) WHERE r.task_id=? "
            "AND a.kind IN ('action_result','extension_candidate','diff','test_report',"
            "'task_file_input','task_file_manifest','file_work_receipt','file_requirement_binding',"
            "'artifact_creation_input','artifact_creation_code','artifact_creation_receipt')",
            (task["task_id"],)).fetchone():
        raise ValueError(reason)
    worked = False
    for step in db.execute("SELECT s.* FROM agent_steps s JOIN agent_runs r USING(run_id) WHERE r.task_id=?",
                           (task["task_id"],)):
        safe_kind = (step["kind"] in {"plan", "verify", "summary"}
            or step["kind"] == "specialist" and step["specialist_profile"] in SP.RESEARCH_PROFILES
            or step["kind"] == "capability" and step["capability"] in _LOCAL_TOOLS | {"document_extract_text"})
        if (not safe_kind or step["state"] not in {"succeeded", "skipped"}
                or step["finished_at"] is None or step["commit_ref"]):
            raise ValueError(reason)
        worked |= step["run_id"] == parent["run_id"] and step["kind"] in {"specialist", "capability"} and step["state"] == "succeeded"
    if not worked or not parent["result_summary"].strip():
        raise ValueError(reason)


def _completed_native_task(db, task, parent, current):
    """Explicit new Owner revision after negative assessment, never a replay.

    A worker's text/step status alone cannot establish a completed native
    invocation. Its latest durable turn and physical cost must agree, and
    every previous cost of this task must be settled.
    """
    from solvio.agent_runtime import provider_switch as PS, specialists as SP
    reason = "followup_native_task_unconfirmed"
    if not all(_has(db, name) for name in ('agent_native_sessions', 'agent_native_turns')):
        raise ValueError(reason)
    if not PS._native_turns_settled(db, task['task_id']):
        raise ValueError(reason)
    workers = db.execute("SELECT * FROM agent_steps WHERE run_id=? AND kind='specialist' ORDER BY seq,attempt",
                         (parent['run_id'],)).fetchall()
    # Either native worker of the same order path (N8/C4 §2.2): the step's
    # profile names the provider, and session, turn and physical claim must
    # all carry THAT provider — never a literal pin on one of them.
    providers = {profile: provider for provider, profile in SP.WORKER_PROFILES.items()}
    if (len(workers) not in (1, 2) or any(worker['specialist_profile'] not in providers
            or worker['state'] != 'succeeded' or worker['finished_at'] is None for worker in workers)):
        raise ValueError(reason)
    if len(workers) == 2:
        reworks = db.execute("SELECT seq FROM agent_steps WHERE run_id=? AND kind='verify' "
                            "AND outcome_reason='task_reworked'", (parent['run_id'],)).fetchall()
        if len(reworks) != 1 or not workers[0]['seq'] < reworks[0]['seq'] < workers[1]['seq']:
            raise ValueError(reason)
    natives = db.execute("SELECT t.*,s.task_id,s.profile,s.provider,s.native_thread_id "
        "FROM agent_native_turns t JOIN agent_native_sessions s USING(session_id) "
        "WHERE s.task_id=? ORDER BY t.rowid DESC", (task['task_id'],)).fetchall()
    costs = db.execute("SELECT i.*,c.state AS settlement,c.actual_cents,c.subject_id AS cost_subject "
        "FROM agent_provider_invocations i LEFT JOIN agent_cost_reservations c "
        "ON c.reservation_id=i.reservation_id WHERE i.task_id=?", (task['task_id'],)).fetchall()
    # The existing native-session admission also permits a proved non-start:
    # released money is resolved money. The shared check above still refuses
    # any native turn whose outcome is unknown, including such an older attempt.
    if not costs or any(row['finished_at'] is None or row['cost_subject'] != task['task_id']
            or not ((row['state'] == 'finished' and row['settlement'] == 'settled'
                     and type(row['actual_cents']) is int)
                    or (row['state'] == 'not_dispatched' and row['settlement'] == 'released'))
            for row in costs):
        raise ValueError(reason)
    claims = {row['invocation_id']: row for row in costs}
    for worker in workers:
        profile = worker['specialist_profile']
        provider = providers[profile]
        native = next((row for row in natives if row['invocation_id'] in claims
            and claims[row['invocation_id']]['operation_id'] == worker['step_id']), None)
        if (not native or (native['run_id'], native['profile'], native['provider'], native['state'],
                native['terminal_status'], native['revision'], native['revision_digest']) !=
                (parent['run_id'], profile, provider, 'terminal', 'completed',
                 current['revision'], current['digest'])
                or not native['native_thread_id'] or not native['native_turn_id']):
            raise ValueError(reason)
        claim = claims[native['invocation_id']]
        if ((claim['run_id'], claim['phase'], claim['provider'],
                claim['reservation_id'], claim['request_digest']) !=
                (parent['run_id'], 'specialist', provider, native['reservation_id'], native['request_digest'])):
            raise ValueError(reason)
    # Neither a later revision nor an unrelated turn may be hidden by selecting
    # the workers' old receipts. The last worker must still own the latest turn.
    if natives[0]['invocation_id'] != native['invocation_id']:
        raise ValueError(reason)


def _eligible(db, task, parent, request, receipt, chain, *, prepared_run_id=""):
    if receipt is not None:
        _receipt(receipt, task)
    if task["scope"] not in {S.SCOPE_RESEARCH, S.SCOPE_TASK} or task["target_repo"]:
        raise ValueError("followup_scope_not_supported")
    if task["scope"] == S.SCOPE_TASK and request.get("input_artifact_ids"):
        raise ValueError("native_followup_uses_bound_workspace")
    completed = task["state"] == S.TASK_COMPLETED and parent["state"] == S.SUCCEEDED
    failed_research = (task["state"] == S.TASK_FAILED and parent["state"] == S.FAILED
                       and parent["failure_category"] == "goal_unverified")
    if not (completed or failed_research) or parent["finished_at"] is None:
        raise ValueError("followup_requires_completed_task")
    current = _current(db, task, parent, chain)
    if (request["expected_revision"] != current["revision"] or request["expected_digest"] != current["digest"]
            or chain and chain[-1][0]["run_id"] != parent["run_id"]):
        raise ValueError("followup_revision_changed")
    base_id = chain[0][1]["base_run_id"] if chain else parent["run_id"]
    known = {base_id} | {r["run_id"] for r, _ in chain}
    for run in db.execute("SELECT * FROM agent_runs WHERE task_id=?", (task["task_id"],)):
        if run["run_id"] == prepared_run_id:
            continue
        if run["state"] not in S.TERMINAL_STATES or run["finished_at"] is None:
            raise ValueError("followup_active_run")
        if run["run_id"] not in known:
            raise ValueError("followup_untracked_runs")
    # Narrow first scope: previously authenticated research/document work only.
    if not all(_has(db, name) for name in ("agent_task_sources", "agent_task_grants", "agent_cost_policies", "agent_provider_invocations")):
        raise ValueError("followup_original_authority_required")
    source = db.execute("SELECT * FROM agent_task_sources WHERE task_id=? AND run_id=?", (task["task_id"], base_id)).fetchone()
    grant = db.execute("SELECT * FROM agent_task_grants WHERE run_id=?", (base_id,)).fetchone()
    if (not source or source["run_id"] != base_id or source["state"] != "ready" or not grant
            or source["authorizer"] != task["created_principal"] or grant["authorizer"] != source["authorizer"]
            or grant["receipt_reference"] != source["receipt_reference"] or grant["receipt_method"] != source["receipt_method"]
            or sorted(json.loads(grant["capabilities"]), key=_json) != sorted(json.loads(source["capabilities"]), key=_json)
            or grant["task_id"] != task["task_id"]):
        raise ValueError("followup_original_authority_required")
    fingerprint = _task_fingerprint(task)
    if (grant["task_fingerprint"] != fingerprint or grant["binding_digest"] != _grant_binding(
            task_id=task["task_id"], run_id=base_id, fingerprint=fingerprint, method=grant["receipt_method"],
            receipt=grant["receipt_reference"], authorizer=grant["authorizer"], capabilities=json.loads(grant["capabilities"]),
            expires_at=grant["expires_at"])):
        raise ValueError("followup_original_authority_changed")
    if current["revision"] > 1:
        from solvio.agent_runtime.task_authority import _run_fingerprint
        parent_body = next(b for r, b in chain if r["run_id"] == parent["run_id"])
        parent_grant = db.execute("SELECT * FROM agent_task_grants WHERE run_id=?", (parent["run_id"],)).fetchone()
        expected_receipt = parent_body["receipt"]
        parent_fingerprint = _run_fingerprint(db, task, parent["run_id"])
        if (parent_grant is None or parent_grant["task_id"] != task["task_id"]
                or parent_grant["task_fingerprint"] != parent_fingerprint
                or parent_grant["receipt_method"] != expected_receipt["method"]
                or parent_grant["receipt_reference"] != expected_receipt["reference"]
                or parent_grant["authorizer"] != expected_receipt["authorizer"]
                or parent_grant["binding_digest"] != _grant_binding(task_id=task["task_id"],
                    run_id=parent["run_id"], fingerprint=parent_fingerprint, method=parent_grant["receipt_method"],
                    receipt=parent_grant["receipt_reference"], authorizer=parent_grant["authorizer"],
                    capabilities=json.loads(parent_grant["capabilities"]), expires_at=parent_grant["expires_at"])):
            raise ValueError("followup_parent_authority_required")
    if any(g[0] is not None for g in db.execute("SELECT revoked_at FROM agent_task_grants WHERE task_id=?", (task["task_id"],))):
        raise ValueError("followup_authority_revoked")
    if not db.execute("SELECT 1 FROM agent_cost_policies WHERE subject_id=?", (task["task_id"],)).fetchone():
        raise ValueError("followup_cost_policy_required")
    if db.execute("SELECT 1 FROM agent_provider_invocations WHERE task_id=? AND (state IN ('claimed','unknown') OR (state='finished' AND finished_at IS NULL))", (task["task_id"],)).fetchone():
        raise ValueError("followup_previous_invocation_unresolved")
    if db.execute("SELECT 1 FROM agent_cost_reservations WHERE subject_id=? AND state IN ('reserved','unknown')", (task["task_id"],)).fetchone():
        raise ValueError("followup_previous_cost_unresolved")
    for table in ("agent_action_contracts", "agent_portal_connections"):
        if _has(db, table) and db.execute("SELECT 1 FROM " + table + " WHERE task_id=?", (task["task_id"],)).fetchone():
            raise ValueError("followup_external_effect_not_supported")
    for step in db.execute("SELECT s.* FROM agent_steps s JOIN agent_runs r USING(run_id) WHERE r.task_id=?", (task["task_id"],)):
        if step["state"] in {"pending", "running", "waiting", "unknown"}:
            raise ValueError("followup_previous_step_unresolved")
        if step["approval_id"] or step["execution_id"] or (step["kind"] == "capability" and step["capability"] not in _READ_CAPABILITIES):
            raise ValueError("followup_external_effect_not_supported")
    requirements = task["requirements"] if current["revision"] == 1 else _requirements(db, parent["run_id"], current["effective_objective"])
    if RQ.load(requirements, objective=current["effective_objective"]) is None:
        raise ValueError("followup_parent_requirements_unbound")
    if failed_research:
        if task['scope'] == S.SCOPE_TASK:
            _completed_native_task(db, task, parent, current)
        else:
            _failed_research_readonly(db, task, parent, chain, prepared_run_id=prepared_run_id)
    return current, base_id


def eligibility(ledger, run_id) -> dict:
    """Fresh read-only eligibility, without manufacturing an Owner receipt."""
    try:
        with ledger._open() as db:
            task, run = _task_run(db, run_id)
            chain = _chain(db, task)
            current = _current(db, task, run, chain)
            if current["revision"] >= MAX_REVISIONS:
                raise ValueError("followup_revision_limit")
            _eligible(db, task, run, {"expected_revision": current["revision"],
                "expected_digest": current["digest"]}, None, chain)
        _file_parent_evidence(ledger, run_id)
    except (ValueError, TypeError, KeyError) as exc:
        return {"eligible": False, "reason": str(exc) if type(exc) is ValueError else "followup_binding_invalid"}
    return {"eligible": True, "reason": ""}


def _file_parent_evidence(ledger, parent_run_id):
    """A succeeded file step needs the actual native readback, not its label."""
    steps = [s for s in ledger.steps_for_run(parent_run_id) if s.capability == "file_process" and s.state == "succeeded"]
    if not steps:
        return
    from solvio.agent_runtime import file_results as FR
    confirmed = {item.artifact_id for item in FR.completion_evidence(ledger, parent_run_id)}
    if not confirmed or any(not confirmed.intersection(step.artifact_refs) for step in steps):
        raise ValueError("followup_parent_file_unverified")


def _snapshots(ledger, run_id, ids):
    out = []
    total = 0
    for artifact_id in ids:
        # Cheap metadata bound prevents reading a known 128-MiB result first.
        artifact = next((a for a in ledger.artifacts_for_run(run_id) if a.artifact_id == artifact_id), None)
        if artifact is None or artifact.bytes < 1 or total + artifact.bytes > MAX_INPUT_BYTES:
            raise ValueError("followup_input_unavailable_or_too_large")
        descriptor, content = F.read_result(ledger, run_id, artifact_id)
        total += len(content)
        if descriptor["id"] != artifact_id or total > MAX_INPUT_BYTES:
            raise ValueError("followup_input_unavailable_or_too_large")
        out.append(RevisionInput(_json(descriptor), content))
    return tuple(out)


def prepare(ledger, value, receipt: VerifiedTaskReceipt) -> PreparedFollowup:
    request = canonical_followup(value)
    with ledger._open() as db:
        task, parent = _task_run(db, request["run_id"])
        _receipt(receipt, task)
        chain = _chain(db, task)
        prior = _prior(db, task, request, receipt, chain)
        if prior is not None:
            row, body = next((r, b) for r, b in chain if r["run_id"] == prior["run_id"])
            binding = {key: body[key] for key in ("base_run_id", "original_digest", "parent_result", "effective_objective")}
            # The fresh authenticated receipt recovers the immutable request;
            # admission retains the originally recorded receipt and grant.
            return PreparedFollowup(os.path.realpath(ledger.path), _json(request), task["task_id"], receipt,
                                    _json(binding), _read_inputs(db, row, body))
        _, base_id = _eligible(db, task, parent, request, receipt, chain)
        _, base = _task_run(db, base_id)
        binding = {"base_run_id": base_id, "original_digest": _original_digest(db, task, base),
            "parent_result": _parent_result(parent), "effective_objective": _objective(task["objective"],
                [b["request"]["text"] for _, b in chain] + [request["text"]])}
        task_id = task["task_id"]
    _file_parent_evidence(ledger, parent["run_id"])
    return PreparedFollowup(os.path.realpath(ledger.path), _json(request), task_id, receipt,
                            _json(binding), _snapshots(ledger, parent["run_id"], request["input_artifact_ids"]))


def _locked(db, ledger):
    """Require the caller's existing SQLite writer lock, not merely BEGIN."""
    paths = {os.path.realpath(row[2]) for row in db.execute("PRAGMA database_list") if row[1] == "main"}
    if not db.in_transaction or paths != {os.path.realpath(ledger.path)}:
        raise ValueError("followup_immediate_transaction_required")
    probe = sqlite3.connect(ledger.path, isolation_level=None, timeout=0)
    try:
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY:
                raise
        else:
            probe.rollback()
            raise ValueError("followup_immediate_transaction_required")
        # A different writer cannot masquerade as this caller's lock.
        db.execute("UPDATE agent_tasks SET updated_at=updated_at WHERE 0")
    finally:
        probe.close()


def record_admitted(db, ledger, prepared: PreparedFollowup, *, run_id: str) -> dict:
    """Caller must prepare Run + source/grant gating atomically; NO commit here.

    The task must still have an eligible terminal parent. Reopening the task
    is a separate caller-owned operation; the old run remains terminal.
    A failed call requires caller rollback.
    """
    _locked(db, ledger)
    _run_id(run_id)
    if type(prepared) is not PreparedFollowup or prepared.ledger_path != os.path.realpath(ledger.path):
        raise ValueError("followup_preparation_invalid")
    request = canonical_followup(prepared.request)
    task, parent = _task_run(db, request["run_id"])
    _receipt(prepared.receipt, task)
    chain = _chain(db, task)
    prior = _prior(db, task, request, prepared.receipt, chain)
    if prior is not None:
        if run_id != prior["run_id"]:
            # A caller that inserted a new run before checking replay must
            # roll back, rather than commit an unreferenced second child.
            raise ValueError("followup_replay_requires_original_run")
        return prior
    current, base_id = _eligible(db, task, parent, request, prepared.receipt, chain, prepared_run_id=run_id)
    _, new_run = _task_run(db, run_id)
    if (new_run["task_id"] != task["task_id"] or new_run["parent_run_id"] != parent["run_id"]
            or new_run["state"] != S.CREATED or new_run["started_at"] is not None or new_run["finished_at"] is not None
            or db.execute("SELECT 1 FROM agent_steps WHERE run_id=?", (run_id,)).fetchone()):
        raise ValueError("followup_prepared_run_required")
    _, base = _task_run(db, base_id)
    binding = {"base_run_id": base_id, "original_digest": _original_digest(db, task, base),
        "parent_result": _parent_result(parent), "effective_objective": _objective(task["objective"],
            [b["request"]["text"] for _, b in chain] + [request["text"]])}
    if prepared.task_id != task["task_id"] or prepared.binding_json != _json(binding):
        raise ValueError("followup_preparation_changed")
    _file_parent_evidence(ledger, parent["run_id"])
    inputs = _snapshots(ledger, parent["run_id"], request["input_artifact_ids"])
    if inputs != prepared.inputs:
        raise ValueError("followup_input_changed")
    body = {**binding, "task_id": task["task_id"], "run_id": run_id, "request": request,
        "receipt": {"method": prepared.receipt.method, "reference": prepared.receipt.reference,
                    "authorizer": prepared.receipt.authorizer}, "inputs": [i.descriptor for i in inputs]}
    digest = _digest("SOLVIO_TASK_REVISION_V1", body)
    reference, now = "tr-" + digest[:32], time.time()
    db.execute("INSERT INTO agent_task_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (reference, task["task_id"], run_id, parent["run_id"], current["revision"] + 1,
         prepared.receipt.authorizer, request["client_request_id"], prepared.request_digest,
         prepared.receipt.method, prepared.receipt.reference, _json(body), digest, now))
    for item in inputs:
        db.execute("INSERT INTO agent_task_revision_inputs VALUES (?,?,?,?)",
            (reference, item.descriptor["id"], item.descriptor_json, item.content))
    row = db.execute("SELECT * FROM agent_task_revisions WHERE reference=?", (reference,)).fetchone()
    return _view(row, body)


def _file_request(inputs):
    from solvio.agent_runtime import file_inputs as FI
    if not inputs:
        return None
    if any(not item.descriptor["name"].lower().endswith((".csv", ".xlsx")) for item in inputs):
        raise ValueError("followup_input_format_not_supported")
    return FI.FileTaskRequest(tuple(FI.FileInput(item.descriptor["name"], item.content) for item in inputs))


def prepared_file_request(prepared: PreparedFollowup):
    if type(prepared) is not PreparedFollowup:
        raise ValueError("followup_preparation_invalid")
    return _file_request(prepared.inputs)


def _revision_record(db, task, run_id):
    return next(((row, body) for row, body in _chain(db, task) if row["run_id"] == run_id), None)


def _artifact_grant():
    from solvio.agent_runtime.artifact_creation import capability_grant
    grant = capability_grant()
    return {"name": grant.name, "version": grant.version, "constraints": grant.constraints}


def source_descriptor(db, task_id, run_id, receipt, allowed) -> dict:
    """Exact source digest material, derived only from persisted Owner inputs."""
    from solvio.agent_runtime import file_inputs as FI
    task, run = _task_run(db, run_id)
    match = _revision_record(db, task, run_id)
    if match is None or task["task_id"] != task_id:
        raise ValueError("task_revision_source_missing")
    row, body = match
    _receipt(receipt, task)
    if {"method": receipt.method, "reference": receipt.reference, "authorizer": receipt.authorizer} != body["receipt"]:
        raise ValueError("task_revision_receipt_changed")
    request = _file_request(_read_inputs(db, row, body))
    canonical_allowed = json.loads(_json(allowed))
    if request is not None:
        raw = FI._json(FI._manifest(request))
        digest = FI._hash(raw)
        expected = FI.BoundFileTask(task_id, run_id, FI._artifact_id(run_id, "manifest", digest), digest, len(raw)).capability_grant
        entries = [{"name": expected.name, "version": expected.version, "constraints": expected.constraints}]
        if canonical_allowed != entries:
            raise ValueError("task_revision_capabilities_changed")
    else:
        options = [{"name": name, "version": 1, "constraints": {}} for name in sorted(_LOCAL_TOOLS)] + [_artifact_grant()]
        if (type(canonical_allowed) is not list or len(canonical_allowed) > len(options)
                or len({_json(item) for item in canonical_allowed}) != len(canonical_allowed)
                or any(item not in options for item in canonical_allowed)):
            raise ValueError("task_revision_capabilities_changed")
    policy = db.execute("SELECT ask_threshold_cents FROM agent_cost_policies WHERE subject_id=?", (task_id,)).fetchone()
    if policy is None:
        raise ValueError("followup_cost_policy_required")
    return {"kind": "task_followup_v1", "task_id": task_id, "run_id": run_id,
        "parent_run_id": run["parent_run_id"], "revision_digest": row["body_digest"],
        "original_task": _original(db, task, _task_run(db, body["base_run_id"])[1]),
        "effective_objective": body["effective_objective"], "followup": body["request"],
        "receipt": body["receipt"], "origin": {"app_session": "trusted_interactive_app",
            "dashboard_session": "trusted_dashboard"}[receipt.method],
        "capabilities": canonical_allowed, "cost_threshold_cents": policy[0],
        "file_request": request.descriptor if request is not None else None}


def validate_source(db, task_id, run_id, receipt, allowed) -> bool:
    """Validate a revision source under the caller's current transaction.

    False means an original run and preserves its existing validator. A new
    marked source without its revision, or a revision without its source, is
    always an error; it cannot fall back to original-task authority.
    """
    from solvio.agent_runtime.task_start_service import _digest as source_digest
    task, run = _task_run(db, run_id)
    source = db.execute("SELECT * FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone() if _has(db, "agent_task_sources") else None
    match = _revision_record(db, task, run_id)
    marker = source["task_revision_digest"] if source is not None and "task_revision_digest" in source.keys() else ""
    if match is None:
        if marker:
            raise ValueError("task_revision_source_missing")
        if run["parent_run_id"] and _has(db, "agent_task_sources") and db.execute(
                "SELECT 1 FROM agent_task_sources WHERE task_id=?", (task_id,)).fetchone():
            raise ValueError("task_revision_source_missing")
        return False
    row, _ = match
    if source is None or marker != row["body_digest"]:
        raise ValueError("task_revision_source_changed")
    descriptor = source_descriptor(db, task_id, run_id, receipt, allowed)
    if (source["task_id"] != task_id or source["principal"] != receipt.authorizer
            or source["authorizer"] != receipt.authorizer or source["receipt_method"] != receipt.method
            or source["receipt_reference"] != receipt.reference
            or source["request_id"] != row["request_id"]
            or source["request_digest"] != source_digest(descriptor)
            or source["cost_threshold_cents"] != descriptor["cost_threshold_cents"]
            or json.loads(source["capabilities"]) != descriptor["capabilities"]
            or source["action_intent_digest"] or source["state"] not in {"preparing", "ready"}):
        raise ValueError("task_revision_source_changed")
    return True


def grant_revision_digest(db, task, run_id) -> str:
    source = db.execute("SELECT * FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone() if _has(db, "agent_task_sources") else None
    match = _revision_record(db, task, run_id)
    if match is None:
        # Reuse the closed missing-marker checks, without inventing a receipt
        # for the legacy path that has no source at all.
        if source is not None and "task_revision_digest" in source.keys() and source["task_revision_digest"]:
            raise ValueError("task_revision_source_missing")
        _, run = _task_run(db, run_id)
        if run["parent_run_id"] and _has(db, "agent_task_sources") and db.execute(
                "SELECT 1 FROM agent_task_sources WHERE task_id=?", (task["task_id"],)).fetchone():
            raise ValueError("task_revision_source_missing")
        return ""
    if source is None:
        raise ValueError("task_revision_source_missing")
    receipt = VerifiedTaskReceipt(source["receipt_method"], source["receipt_reference"], source["authorizer"])
    validate_source(db, task["task_id"], run_id, receipt, json.loads(source["capabilities"]))
    return match[0]["body_digest"]


def bind_requirements(ledger, run_id, payload: str) -> bool:
    """Set once for a revision only; original requirements use the old seam."""
    with ledger._open() as db:
        db.execute("BEGIN IMMEDIATE")
        task, run = _task_run(db, run_id)
        chain = _chain(db, task)
        match = next(((row, body) for row, body in chain if row["run_id"] == run_id), None)
        if match is None:
            raise ValueError("task_revision_required")
        if run["state"] in S.TERMINAL_STATES or run["finished_at"] is not None:
            raise ValueError("task_revision_terminal")
        objective = match[1]["effective_objective"]
        if type(payload) is not str or len(payload) > S.MAX_PLAN_CHECKPOINT or RQ.load(payload, objective=objective) is None:
            raise ValueError("invalid_task_revision_requirements")
        if S._safe_json_record(payload, S.MAX_PLAN_CHECKPOINT, where="task_revision.requirements") != payload:
            raise ValueError("task_revision_requirements_would_change")
        if db.execute("SELECT 1 FROM agent_task_revision_requirements WHERE run_id=?", (run_id,)).fetchone():
            return False
        db.execute("INSERT INTO agent_task_revision_requirements VALUES (?,?,?)",
            (run_id, payload, _digest("SOLVIO_TASK_REVISION_REQUIREMENTS_V1", payload)))
        return True
