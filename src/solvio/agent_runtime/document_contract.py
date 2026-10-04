"""One authenticated input and its fixed, implementation-independent contract.

The document is data, never an instruction or a source of capability authority.
Only the public task entrance creates DocumentRequest from the exact authenticated
body. Input files are durable before the existing task/source transaction commits;
the same ledger owns their artifact row and the immutable task grant. No model
argument, saved path or adapter result can introduce another input resource.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import re
import stat

from solvio.agent_runtime import store as S
from solvio.agent_runtime.task_authority import CapabilityGrant, TaskAuthority
from solvio.agent_runtime import document_formats as DF

CAPABILITY = "document_extract_text"
VERSION = 1
CONTRACT = "rtf_text_v1"
RESOURCE = "input_document"
MAX_BYTES = 65_536
MAX_OUTPUT_BYTES = 65_536
MAX_INPUT_BYTES = DF.MAX_INPUT_BYTES
_FILE = "input-document.rtf"
_CONTRACT_BODY = {
    "contract": CONTRACT, "capability": CAPABILITY, "version": VERSION,
    "operation": "extract_text", "input_format": "rtf", "output_encoding": "utf-8",
    "max_input_bytes": MAX_BYTES, "max_output_bytes": MAX_OUTPUT_BYTES,
    "input_immutable": True, "network": False, "external_writes": False,
    "local_adapter_development": True,
    "native_executable": "/usr/bin/textutil",
    "native_arguments": ["-format", "rtf", "-convert", "txt", "-stdin", "-stdout",
                         "-encoding", "UTF-8"],
}
CONTRACT_DIGEST = hashlib.sha256(b"SOLVIO_DOCUMENT_CONTRACT_V1\0" + json.dumps(
    _CONTRACT_BODY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
_ARGUMENTS = frozenset({"contract_digest", "resource_id", "source_sha256", "source_bytes"})


def profile(input_format):
    # Preserve the original RTF body, digest, filename and grant arguments.
    if input_format == "rtf":
        return DF.FormatProfile("rtf", CONTRACT, MAX_BYTES, _FILE,
                                tuple(_CONTRACT_BODY["native_arguments"]), CONTRACT_DIGEST)
    return DF.profile(input_format)


def _profile_for_digest(digest):
    selected = next((item for item in (profile("rtf"), *DF.PROFILES)
                     if item.contract_digest == digest), None)
    if selected is None:
        raise ValueError("invalid_document_grant")
    return selected


@dataclass(frozen=True)
class DocumentRequest:
    content: bytes = field(repr=False)
    format: str = "rtf"

    def __post_init__(self):
        if self.format != "rtf":
            DF.validate(self.content, self.format)
            return
        if (type(self.content) is not bytes or not 1 <= len(self.content) <= MAX_BYTES
                or not re.match(br"\{\\rtf1(?:\\|[ \t\r\n])", self.content)):
            raise ValueError("invalid_rtf_document")

    @property
    def descriptor(self):
        return {"operation": "extract_text", "format": self.format,
                "sha256": hashlib.sha256(self.content).hexdigest(), "bytes": len(self.content)}


def validate_request(value) -> DocumentRequest:
    """The only accepted wire form. No paths, model names or supplied digests."""
    if (type(value) is not dict or set(value) != {"operation", "format", "content_b64"}
            or value["operation"] != "extract_text" or type(value["format"]) is not str
            or type(value["content_b64"]) is not str
            or len(value["content_b64"]) > 4 * ((MAX_INPUT_BYTES + 2) // 3)):
        raise ValueError("invalid_document_request")
    selected = profile(value["format"])
    if len(value["content_b64"]) > 4 * ((selected.max_input_bytes + 2) // 3):
        raise ValueError("invalid_document_request")
    try:
        content = base64.b64decode(value["content_b64"], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid_document_encoding") from None
    if base64.b64encode(content).decode("ascii") != value["content_b64"]:
        raise ValueError("noncanonical_document_encoding")
    return DocumentRequest(content, value["format"])


def canonical_request(value) -> dict:
    request = validate_request(value)
    return {"operation": "extract_text", "format": request.format,
            "content_b64": base64.b64encode(request.content).decode("ascii")}


@dataclass(frozen=True)
class BoundDocument:
    task_id: str
    run_id: str
    resource_id: str
    source_sha256: str
    source_bytes: int
    grant_reference: str = ""
    format: str = "rtf"

    @property
    def contract(self):
        return profile(self.format).contract

    @property
    def filename(self):
        return profile(self.format).filename

    @property
    def max_input_bytes(self):
        return profile(self.format).max_input_bytes

    @property
    def contract_digest(self):
        return profile(self.format).contract_digest

    @property
    def arguments(self):
        return {"contract_digest": self.contract_digest, "resource_id": self.resource_id,
                "source_sha256": self.source_sha256, "source_bytes": self.source_bytes}

    @property
    def capability_grant(self):
        return CapabilityGrant(CAPABILITY, VERSION, self.arguments)


def _artifact_id(run_id, digest):
    return "aa-" + hashlib.sha256((run_id + "\0" + digest).encode()).hexdigest()[:16]


def _root():
    # state_dir is Core configuration. Every child below it is opened with
    # NOFOLLOW; the artifact/run directories cannot redirect document access.
    return os.path.realpath(S.state_dir())


def _path(run_id, input_format="rtf"):
    return os.path.join(_root(), S.ARTIFACT_DIRNAME, run_id, profile(input_format).filename)


def _directory(run_id, *, create=False):
    if not isinstance(run_id, str) or not re.fullmatch(r"ar-[a-f0-9]{16}", run_id):
        raise ValueError("invalid_document_run")
    root = _root()
    if create:
        os.makedirs(root, mode=0o700, exist_ok=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in (S.ARTIFACT_DIRNAME, run_id):
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def prepare(request: DocumentRequest, *, task_id, run_id) -> BoundDocument:
    """Core-only, before task commit. Never overwrite an existing input file."""
    if type(request) is not DocumentRequest:
        raise ValueError("typed_document_request_required")
    request.__post_init__()
    source = request.descriptor
    bound = BoundDocument(task_id, run_id, _artifact_id(run_id, source["sha256"]),
                          source["sha256"], source["bytes"], format=request.format)
    directory = _directory(run_id, create=True)
    try:
        file = os.open(bound.filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=directory)
        with os.fdopen(file, "wb") as stream:
            stream.write(request.content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return bound


def record_prepared(connection, bound: BoundDocument, *, now):
    """Use the SAME transaction as task/run/source creation."""
    connection.execute("INSERT INTO agent_artifacts "
        "(artifact_id,run_id,kind,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?)",
        (bound.resource_id, bound.run_id, "task_input", _path(bound.run_id, bound.format),
         bound.source_sha256, bound.source_bytes, now))


def _from_entries(task_id, run_id, entries) -> BoundDocument | None:
    matches = [entry for entry in entries if entry.name == CAPABILITY]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("duplicate_document_grant")
    entry = matches[0]
    args = entry.constraints
    selected = _profile_for_digest(args.get("contract_digest")) if type(args) is dict else None
    if (entry.version != VERSION or type(args) is not dict or set(args) != _ARGUMENTS
            or selected is None
            or type(args["source_bytes"]) is not int or not 1 <= args["source_bytes"] <= selected.max_input_bytes
            or not isinstance(args["source_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", args["source_sha256"])
            or args["resource_id"] != _artifact_id(run_id, args["source_sha256"])):
        raise ValueError("invalid_document_grant")
    return BoundDocument(task_id, run_id, args["resource_id"], args["source_sha256"], args["source_bytes"],
                         format=selected.format)


def _read_bound(connection, bound):
    row = connection.execute("SELECT * FROM agent_artifacts WHERE artifact_id=? AND run_id=?",
                             (bound.resource_id, bound.run_id)).fetchone()
    if (row is None or row["kind"] != "task_input" or row["path"] != _path(bound.run_id, bound.format)
            or row["sha256"] != bound.source_sha256 or row["bytes"] != bound.source_bytes):
        raise ValueError("document_artifact_binding_changed")
    directory = _directory(bound.run_id)
    try:
        file = os.open(bound.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(file, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_mode & 0o277 or info.st_size != bound.source_bytes):
                raise ValueError("document_not_immutable_regular_file")
            content = stream.read(bound.max_input_bytes + 1)
    finally:
        os.close(directory)
    if len(content) != bound.source_bytes or hashlib.sha256(content).hexdigest() != bound.source_sha256:
        raise ValueError("document_content_changed")
    DocumentRequest(content, bound.format)
    return content


def check_prepared(ledger, *, task_id, run_id, entries):
    """Validate the durable source before grant creation/ready; no authority minted here."""
    bound = _from_entries(task_id, run_id, entries)
    if bound is not None:
        with ledger._open() as connection:
            _read_bound(connection, bound)
    return bound


def for_run(ledger, run_id) -> BoundDocument | None:
    """Current verified grant and canonical resource; absent grant is not permission."""
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
            raise ValueError("document_authority_invalid:" + allowed.reason)
        _read_bound(connection, bound)
    return replace(bound, grant_reference=grant.reference)


def read_for_run(ledger, run_id, *, arguments=None) -> bytes:
    """Recheck the actual grant, exact arguments and bytes immediately before use."""
    bound = for_run(ledger, run_id)
    if bound is None:
        raise ValueError("document_not_granted")
    if arguments is not None:
        from solvio.agent_runtime.task_authority import _canonical
        if _canonical(arguments) != _canonical(bound.arguments):
            raise ValueError("document_arguments_changed")
    authority = TaskAuthority(ledger)
    with ledger._open() as connection:
        connection.execute("BEGIN")
        allowed = authority._verify(connection, bound.grant_reference, CAPABILITY,
            bound.arguments, VERSION, task_id=bound.task_id, run_id=run_id)
        if not allowed.allowed:
            raise ValueError("document_authority_invalid:" + allowed.reason)
        return _read_bound(connection, bound)
