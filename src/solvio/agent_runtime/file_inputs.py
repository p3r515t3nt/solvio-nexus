"""Authenticated, immutable input files for an offline file-work contract.

The request carries bytes, never a host path, executor, credential or new
permission. The existing task grant binds the manifest and every source hash.
This module admits and reads data; it neither imports source files nor starts
an agent. Generated tools remain a separate, verified implementation.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import re
import time

from solvio.agent_runtime import document_contract as DC, result_files as RF
from solvio.agent_runtime.task_authority import CapabilityGrant, TaskAuthority

CAPABILITY = "file_process"
VERSION = 1
CONTRACT = "offline_file_work_v1"
RESOURCE = "input_files"
MAX_FILES = 4
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 8192
_ARGUMENTS = frozenset({"contract_digest", "resource_id", "source_sha256", "source_bytes"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


CONTRACT_DIGEST = _hash(b"SOLVIO_FILE_WORK_V1\0" + _json({
    "contract": CONTRACT, "capability": CAPABILITY, "version": VERSION,
    "max_files": MAX_FILES, "max_input_bytes": MAX_TOTAL_BYTES,
    "immutable_inputs": True, "network": False, "external_writes": False,
    "generated_code_in_core": False, "outputs": "verified_result_artifacts",
    "development": "existing_native_task_cost_scope",
}))


def _name(value):
    if (not isinstance(value, str) or not value or value != RF.safe_name(value)
            or value in {".", ".."} or len(value.encode("utf-8")) > 240):
        raise ValueError("invalid_file_input_name")
    return value


@dataclass(frozen=True)
class FileInput:
    name: str
    content: bytes = field(repr=False)

    def __post_init__(self):
        _name(self.name)
        if type(self.content) is not bytes or not 1 <= len(self.content) <= MAX_FILE_BYTES:
            raise ValueError("invalid_file_input_size")

    @property
    def descriptor(self):
        return {"name": self.name, "sha256": _hash(self.content), "bytes": len(self.content)}


@dataclass(frozen=True)
class FileTaskRequest:
    files: tuple[FileInput, ...] = field(repr=False)

    def __post_init__(self):
        if type(self.files) is not tuple or not 1 <= len(self.files) <= MAX_FILES:
            raise ValueError("invalid_file_input_count")
        names, total = set(), 0
        for item in self.files:
            if type(item) is not FileInput:
                raise ValueError("invalid_file_input")
            item.__post_init__()
            key = item.name.casefold()
            if key in names:
                raise ValueError("duplicate_file_input_name")
            names.add(key)
            total += len(item.content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("file_input_total_limit")

    @property
    def descriptor(self):
        return {"operation": "process_files", "files": [item.descriptor for item in self.files]}


def validate_request(value) -> FileTaskRequest:
    if (type(value) is not dict or set(value) != {"operation", "files"}
            or value["operation"] != "process_files" or type(value["files"]) is not list
            or not 1 <= len(value["files"]) <= MAX_FILES):
        raise ValueError("invalid_file_request")
    result, total = [], 0
    for item in value["files"]:
        if (type(item) is not dict or set(item) != {"name", "content_b64"}
                or type(item["content_b64"]) is not str
                or len(item["content_b64"]) > 4 * ((MAX_FILE_BYTES + 2) // 3)):
            raise ValueError("invalid_file_input")
        try:
            content = base64.b64decode(item["content_b64"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid_file_input_encoding") from None
        if base64.b64encode(content).decode("ascii") != item["content_b64"]:
            raise ValueError("noncanonical_file_input_encoding")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("file_input_total_limit")
        result.append(FileInput(item["name"], content))
    return FileTaskRequest(tuple(result))


def canonical_request(value):
    request = validate_request(value)
    return {"operation": "process_files", "files": [
        {"name": item.name, "content_b64": base64.b64encode(item.content).decode("ascii")}
        for item in request.files]}


def _manifest(request):
    return {"schema": 1, "contract_digest": CONTRACT_DIGEST,
            "files": request.descriptor["files"]}


def _manifest_name():
    return "file-inputs.json"


def _input_name(index):
    return f"file-input-{index}.bin"


def _artifact_id(run_id, role, digest):
    return "aa-" + _hash((run_id + "\0file-work\0" + role + "\0" + digest).encode())[:16]


@dataclass(frozen=True)
class BoundFileTask:
    task_id: str
    run_id: str
    resource_id: str
    source_sha256: str
    source_bytes: int
    grant_reference: str = ""
    contract: str = CONTRACT
    contract_digest: str = CONTRACT_DIGEST
    capability: str = CAPABILITY
    version: int = VERSION
    resource: str = RESOURCE
    max_input_bytes: int = MAX_TOTAL_BYTES

    @property
    def arguments(self):
        return {key: getattr(self, key) for key in _ARGUMENTS}

    @property
    def capability_grant(self):
        return CapabilityGrant(CAPABILITY, VERSION, self.arguments)


@dataclass(frozen=True)
class PreparedFileTask:
    bound: BoundFileTask
    request: FileTaskRequest = field(repr=False)

    @property
    def capability_grant(self):
        return self.bound.capability_grant


def prepare(request, *, task_id, run_id):
    if type(request) is not FileTaskRequest:
        raise ValueError("file_request_required")
    request.__post_init__()
    raw = _json(_manifest(request))
    digest = _hash(raw)
    bound = BoundFileTask(task_id, run_id, _artifact_id(run_id, "manifest", digest), digest, len(raw))
    directory = DC._directory(run_id, create=True)
    try:
        for index, item in enumerate(request.files):
            RF._write_once(directory, _input_name(index), item.content)
        RF._write_once(directory, _manifest_name(), raw)
    finally:
        os.close(directory)
    return PreparedFileTask(bound, request)


def record_prepared(connection, prepared, *, now=None):
    if type(prepared) is not PreparedFileTask:
        raise ValueError("prepared_file_request_required")
    now = time.time() if now is None else now
    bound, request = prepared.bound, prepared.request
    if type(bound) is not BoundFileTask or type(request) is not FileTaskRequest:
        raise ValueError("invalid_prepared_file_request")
    request.__post_init__()
    raw = _json(_manifest(request))
    expected = BoundFileTask(bound.task_id, bound.run_id,
        _artifact_id(bound.run_id, "manifest", _hash(raw)), _hash(raw), len(raw))
    if bound != expected:
        raise ValueError("prepared_file_binding_changed")
    rows = [(bound.resource_id, "task_file_manifest", _manifest_name(), raw)]
    rows.extend((_artifact_id(bound.run_id, str(i), _hash(item.content)), "task_file_input",
                 _input_name(i), item.content) for i, item in enumerate(request.files))
    for aid, kind, filename, content in rows:
        values = (bound.run_id, kind, RF._path(bound.run_id, filename), _hash(content), len(content))
        prior = connection.execute("SELECT run_id,kind,path,sha256,bytes FROM agent_artifacts WHERE artifact_id=?", (aid,)).fetchone()
        if prior is not None and tuple(prior) != values:
            raise ValueError("file_input_record_changed")
        connection.execute("INSERT OR IGNORE INTO agent_artifacts "
            "(artifact_id,run_id,kind,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?)",
            (aid, *values, now))


def _from_entries(task_id, run_id, entries):
    matches = [entry for entry in entries if entry.name == CAPABILITY]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("duplicate_file_grant")
    entry, args = matches[0], matches[0].constraints
    if (entry.version != VERSION or type(args) is not dict or set(args) != _ARGUMENTS
            or args["contract_digest"] != CONTRACT_DIGEST
            or type(args["source_bytes"]) is not int or not 1 <= args["source_bytes"] <= MAX_MANIFEST_BYTES
            or not isinstance(args["source_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", args["source_sha256"])
            or args["resource_id"] != _artifact_id(run_id, "manifest", args["source_sha256"])):
        raise ValueError("invalid_file_grant")
    return BoundFileTask(task_id, run_id, args["resource_id"], args["source_sha256"], args["source_bytes"])


def _read_bound(connection, bound):
    def read(aid, kind, name, size, digest, limit):
        row = connection.execute("SELECT run_id,kind,path,sha256,bytes FROM agent_artifacts WHERE artifact_id=?", (aid,)).fetchone()
        if row is None or tuple(row) != (bound.run_id, kind, RF._path(bound.run_id, name), digest, size):
            raise ValueError("file_input_binding_changed")
        return RF._read_file(directory, name, size, digest, limit=limit)

    directory = DC._directory(bound.run_id)
    try:
        raw = read(bound.resource_id, "task_file_manifest", _manifest_name(), bound.source_bytes,
                   bound.source_sha256, MAX_MANIFEST_BYTES)
        manifest = json.loads(raw)
        if (type(manifest) is not dict or set(manifest) != {"schema", "contract_digest", "files"}
                or manifest["schema"] != 1 or manifest["contract_digest"] != CONTRACT_DIGEST
                or type(manifest["files"]) is not list or not 1 <= len(manifest["files"]) <= MAX_FILES):
            raise ValueError("invalid_file_manifest")
        total, names = 0, set()
        for item in manifest["files"]:
            if (type(item) is not dict or set(item) != {"name", "bytes", "sha256"}
                    or type(item["bytes"]) is not int or not 1 <= item["bytes"] <= MAX_FILE_BYTES
                    or type(item["sha256"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
                raise ValueError("invalid_file_manifest")
            name = _name(item["name"]).casefold()
            if name in names:
                raise ValueError("duplicate_file_input_name")
            names.add(name)
            total += item["bytes"]
        if total > MAX_TOTAL_BYTES:
            raise ValueError("file_input_total_limit")
        files = []
        for index, item in enumerate(manifest["files"]):
            content = read(_artifact_id(bound.run_id, str(index), item["sha256"]),
                "task_file_input", _input_name(index), item["bytes"], item["sha256"], MAX_FILE_BYTES)
            files.append(FileInput(item["name"], content))
        request = FileTaskRequest(tuple(files))
        if _json(_manifest(request)) != raw:
            raise ValueError("file_manifest_changed")
        return request
    finally:
        os.close(directory)


def check_prepared(ledger, *, task_id, run_id, entries):
    bound = _from_entries(task_id, run_id, entries)
    if bound is not None:
        with ledger._open() as connection:
            _read_bound(connection, bound)
    return bound


def validate_issue(connection, task_id, run_id, receipt, allowed):
    """Validate file authority at the actual grant transaction, not just intake.

    No effect for existing non-file tasks. A file grant requires the original
    authenticated source row, its exact capability set and unchanged immutable
    inputs. Removing the file capability from a preparing source is not a way
    to turn that same accepted file task into an ordinary research grant.
    """
    from solvio.agent_runtime.task_revisions import validate_source
    if validate_source(connection, task_id, run_id, receipt, allowed):
        # A revision uses the exact same immutable byte/grant contract. Only
        # its authenticated source descriptor differs from the first intake.
        bound = _from_entries(task_id, run_id, tuple(CapabilityGrant(**item) for item in allowed))
        if bound is not None:
            _read_bound(connection, bound)
        return
    entries = tuple(CapabilityGrant(**item) for item in allowed)
    bound = _from_entries(task_id, run_id, entries)
    source = connection.execute("SELECT * FROM agent_task_sources WHERE run_id=?", (run_id,)).fetchone() \
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_task_sources'").fetchone() else None
    original = json.loads(source["capabilities"]) if source is not None else []
    artifacts = connection.execute("SELECT 1 FROM agent_artifacts WHERE run_id=? "
        "AND kind IN ('task_file_manifest','task_file_input') LIMIT 1", (run_id,)).fetchone()
    if bound is None and not artifacts and not any(item.get("name") == CAPABILITY for item in original):
        return
    task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
    if (bound is None or source is None or task is None or source["task_id"] != task_id
            or task["scope"] != "research" or task["target_repo"]
            or (source["receipt_method"], task["created_origin"]) not in {
                ("app_session", "trusted_interactive_app"), ("dashboard_session", "trusted_dashboard")}
            or source["principal"] != task["created_principal"]
            or source["authorizer"] != task["created_principal"]
            or (receipt.method, receipt.reference, receipt.authorizer) !=
               (source["receipt_method"], source["receipt_reference"], source["authorizer"])
            or _json(sorted(original, key=lambda item: item["name"])) != _json(list(allowed))):
        raise ValueError("file_task_source_changed")
    request = _read_bound(connection, bound)
    from solvio.agent_runtime.task_start_service import _digest
    binding = {"objective": task["objective"], "scope": task["scope"],
        "origin": task["created_origin"], "principal": task["created_principal"],
        "target_repo": task["target_repo"], "conversation_ref": task["conversation_ref"],
        "predecessor_ref": task["predecessor_ref"], "file_request": request.descriptor}
    if source["request_digest"] != _digest(binding):
        raise ValueError("file_task_source_changed")


def for_run(ledger, run_id):
    authority = TaskAuthority(ledger)
    grant = authority.for_run(run_id)
    if grant is None:
        return None
    bound = _from_entries(grant.task_id, run_id, grant.capabilities)
    if bound is None:
        return None
    with ledger._open() as connection:
        connection.execute("BEGIN")
        allowed = authority._verify(connection, grant.reference, CAPABILITY, bound.arguments,
            VERSION, task_id=grant.task_id, run_id=run_id)
        if not allowed.allowed:
            raise ValueError("file_authority_invalid:" + allowed.reason)
        _read_bound(connection, bound)
    return replace(bound, grant_reference=grant.reference)


def read_for_run(ledger, run_id, *, arguments=None):
    bound = for_run(ledger, run_id)
    if bound is None:
        raise ValueError("file_work_not_granted")
    if arguments is not None and _json(arguments) != _json(bound.arguments):
        raise ValueError("file_arguments_changed")
    authority = TaskAuthority(ledger)
    with ledger._open() as connection:
        connection.execute("BEGIN")
        allowed = authority._verify(connection, bound.grant_reference, CAPABILITY, bound.arguments,
            VERSION, task_id=bound.task_id, run_id=run_id)
        if not allowed.allowed:
            raise ValueError("file_authority_invalid:" + allowed.reason)
        return _read_bound(connection, bound)
