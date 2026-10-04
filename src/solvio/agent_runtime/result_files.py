"""Owner deliverables in the existing artifact ledger, with immutable receipts.

Adapters publish bytes, never an executor-supplied download URL or host path.
The completed producing step and readback are required for public delivery.
This proves availability, not semantic quality or an actual Owner download.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import PurePath
import re
import stat
import time
import unicodedata

from solvio.agent_runtime import document_contract as DC, requirements as RQ, store as S

MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_RECEIPT_BYTES = 8192
DELIVERABLE_KINDS = frozenset({"document_result", "result_file"})
PREFIX = "/v1/agent/runs"


def safe_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("invalid_result_name")
    cleaned = "".join(c for c in unicodedata.normalize("NFC", name)
                      if not unicodedata.category(c).startswith("C") and c not in '/\\:"<>|?*')
    cleaned = cleaned.strip(" .")[:120].strip(" .")
    while len(cleaned.encode("utf-8")) > 240:
        cleaned = cleaned[:-1]
    return cleaned or "solvio-datei.bin"


def _media(name: str, content: bytes, requested: str) -> tuple[str, str]:
    """Closed inline types. HTML/SVG and unknown formats remain attachments."""
    if not isinstance(requested, str) or len(requested) > 150:
        raise ValueError("invalid_result_media_type")
    mime = requested.lower().split(";", 1)[0].strip()
    signatures = {
        "image/png": (content.startswith(b"\x89PNG\r\n\x1a\n"), "image"),
        "image/jpeg": (content.startswith(b"\xff\xd8\xff"), "image"),
        "image/gif": (content.startswith((b"GIF87a", b"GIF89a")), "image"),
        "image/webp": (content[:4] == b"RIFF" and content[8:12] == b"WEBP", "image"),
        "application/pdf": (content.startswith(b"%PDF-"), "pdf"),
        "audio/wav": (content[:4] == b"RIFF" and content[8:12] == b"WAVE", "audio"),
        "audio/mpeg": (content.startswith(b"ID3") or len(content) > 1 and content[0] == 255 and content[1] & 224 == 224, "audio"),
        "audio/ogg": (content.startswith(b"OggS"), "audio"),
        "video/mp4": (content[4:8] == b"ftyp", "video"),
        "audio/mp4": (content[4:8] == b"ftyp", "audio"),
        "video/webm": (content.startswith(b"\x1aE\xdf\xa3"), "video"),
    }
    if mime in signatures:
        valid, kind = signatures[mime]
        if not valid:
            raise ValueError("result_media_mismatch")
        return mime, kind
    extension = PurePath(name).suffix.lower()
    if mime in {"text/plain", "text/markdown", "text/csv", "application/json"}:
        content.decode("utf-8")
        if b"\0" in content:
            raise ValueError("result_text_invalid")
        # Serve code/text only as plain text; never make executable active content.
        return "text/plain", "text"
    office = {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".zip": "application/zip",
    }
    if extension in office and mime == office[extension] and content.startswith(b"PK\x03\x04"):
        return mime, "none"
    return "application/octet-stream", "none"


def _path(run_id, name):
    return os.path.join(os.path.realpath(S.state_dir()), S.ARTIFACT_DIRNAME, run_id, name)


def _write_once(directory, filename, content):
    try:
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
    except FileExistsError:
        # Repeating publication of identical bytes does not repeat generation.
        if _read_file(directory, filename, len(content), hashlib.sha256(content).hexdigest(),
                      limit=max(MAX_FILE_BYTES, MAX_RECEIPT_BYTES)) != content:
            raise ValueError("result_publication_changed")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fchmod(stream.fileno(), 0o400)
        os.fsync(stream.fileno())
    os.fsync(directory)


def _read_file(directory, filename, size, digest, *, limit, include_content=True):
    if type(size) is not int or not 0 <= size <= limit or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("result_size_or_hash_invalid")
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o277 or before.st_size != size):
            raise ValueError("result_not_immutable")
        measured, count, chunks = hashlib.sha256(), 0, []
        while chunk := stream.read(min(1024 * 1024, limit + 1 - count)):
            count += len(chunk)
            if count > limit:
                raise ValueError("result_too_large")
            measured.update(chunk)
            if include_content:
                chunks.append(chunk)
        after = os.fstat(stream.fileno())
    if (count != size or measured.hexdigest() != digest
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("result_content_changed")
    return b"".join(chunks) if include_content else None


def _descriptor(run_id, artifact_id, name, mime, size, digest, preview_kind):
    base = f"{PREFIX}/{run_id}/artifacts/{artifact_id}"
    return {"id": artifact_id, "name": name, "mime_type": mime, "size": size,
            "sha256": digest, "preview_kind": preview_kind,
            "download_url": base + "/download",
            "preview_url": base + "/preview" if preview_kind != "none" else None}


def publish_file(ledger, run_id: str, step_id: str, content: bytes, name: str,
                 mime_type: str, requirement: str = "", provider: str = "",
                 billing_mode: str = "", producer_receipt: dict | None = None) -> dict:
    """Core adapter only. Commit file+receipt+step refs together, never finish a step.

    Dispatch authority and cost settlement are the producer's responsibility.
    Publication after cancellation is refused. Public delivery starts only once
    the producing step is succeeded; a crash cannot turn loose bytes into success.
    """
    if type(content) is not bytes or not 1 <= len(content) <= MAX_FILE_BYTES:
        raise ValueError("invalid_result_bytes")
    run = ledger.get_run(run_id)
    step = ledger.get_step(step_id)
    if run is None or run.terminal or step is None or step.run_id != run_id or step.state not in {"running", "succeeded"}:
        raise ValueError("result_producer_invalid")
    from solvio.agent_runtime.task_revisions import task_view
    task = task_view(ledger, run_id)
    bound = RQ.load(task.requirements, objective=task.objective)
    if requirement and (bound is None or requirement not in {
            item["id"] for key in (RQ.ASK, RQ.ACTION, RQ.UNCLEAR) for item in bound[key]}):
        raise ValueError("result_requirement_unbound")
    if any(not isinstance(v, str) or len(v) > 100 for v in (provider, billing_mode, requirement)):
        raise ValueError("result_provenance_invalid")
    name = safe_name(name)
    mime, preview = _media(name, content, mime_type)
    digest = hashlib.sha256(content).hexdigest()
    identity = hashlib.sha256((run_id + "\0" + step_id + "\0" + name + "\0" + digest).encode()).hexdigest()
    artifact_id, receipt_id = "aa-" + identity[:16], "aa-" + identity[16:32]
    filename = f"result-{artifact_id}.bin"
    receipt_name = f"result-{artifact_id}.json"
    receipt = {"schema": 1, "run_id": run_id, "task_id": run.task_id, "step_id": step_id,
        "artifact_id": artifact_id, "name": name, "mime_type": mime, "size": len(content),
        "sha256": digest, "preview_kind": preview, "requirement": requirement,
        "requirements_digest": RQ.digest_of(bound) if bound else "",
        "provider": provider, "billing_mode": billing_mode}
    if producer_receipt is not None:
        if (not isinstance(producer_receipt, dict) or producer_receipt.get("sha256") != digest
                or producer_receipt.get("size") != len(content)
                or producer_receipt.get("mime_type") != mime
                or producer_receipt.get("terminal") != "completed"):
            raise ValueError("result_producer_receipt_invalid")
        fields = ("runtime", "thread_id", "turn_id", "item_id", "terminal", "sha256", "mime_type")
        if any(not isinstance(producer_receipt.get(key), str)
               or not 1 <= len(producer_receipt[key]) <= 160 for key in fields):
            raise ValueError("result_producer_receipt_invalid")
        receipt["producer"] = {key: producer_receipt[key] for key in (*fields, "size")}
    raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    directory = DC._directory(run_id, create=True)
    try:
        _write_once(directory, filename, content)
        _write_once(directory, receipt_name, raw)
    finally:
        os.close(directory)
    with ledger._open() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT state FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        current_step = connection.execute("SELECT state,artifact_refs FROM agent_steps WHERE step_id=? AND run_id=?", (step_id, run_id)).fetchone()
        if not current or current["state"] in S.TERMINAL_STATES or not current_step or current_step["state"] not in {"running", "succeeded"}:
            raise ValueError("result_producer_ended")
        for aid, kind, fname, body in ((artifact_id, "result_file", filename, content),
                                       (receipt_id, "result_receipt", receipt_name, raw)):
            values = (run_id, kind, _path(run_id, fname), hashlib.sha256(body).hexdigest(), len(body))
            prior = connection.execute("SELECT run_id,kind,path,sha256,bytes FROM agent_artifacts WHERE artifact_id=?", (aid,)).fetchone()
            if prior is not None and tuple(prior) != values:
                raise ValueError("result_publication_changed")
            connection.execute("INSERT OR IGNORE INTO agent_artifacts (artifact_id,run_id,kind,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?)",
                               (aid, *values, time.time()))
        refs = json.loads(current_step["artifact_refs"] or "[]")
        refs = list(dict.fromkeys([*refs, artifact_id, receipt_id]))
        connection.execute("UPDATE agent_steps SET artifact_refs=? WHERE step_id=?", (json.dumps(refs), step_id))
    return _descriptor(run_id, artifact_id, name, mime, len(content), digest, preview)


def _verified(ledger, run_id, artifact_id, *, include_content=True):
    rows = ledger.artifacts_for_run(run_id)
    artifact = next((a for a in rows if a.artifact_id == artifact_id and a.kind in DELIVERABLE_KINDS), None)
    if artifact is None:
        raise ValueError("result_unknown")
    if artifact.kind == "document_result":
        from solvio.agent_runtime.document_results import read_result
        content = read_result(ledger, run_id, artifact_id)
        return _descriptor(run_id, artifact_id, "solvio-dokument.txt", "text/plain", len(content),
                           artifact.sha256, "text"), content if include_content else None, None
    filename, receipt_name = f"result-{artifact_id}.bin", f"result-{artifact_id}.json"
    if not re.fullmatch(r"aa-[a-f0-9]{16}", artifact_id) or artifact.path != _path(run_id, filename):
        raise ValueError("result_path_changed")
    proof = next((a for a in rows if a.kind == "result_receipt" and a.path == _path(run_id, receipt_name)), None)
    if proof is None:
        raise ValueError("result_receipt_absent")
    directory = DC._directory(run_id)
    try:
        raw = _read_file(directory, receipt_name, proof.bytes, proof.sha256, limit=MAX_RECEIPT_BYTES)
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            raise ValueError("result_receipt_invalid")
        run = ledger.get_run(run_id)
        from solvio.agent_runtime.task_revisions import task_view
        task = task_view(ledger, run_id) if run else None
        step = ledger.get_step(receipt.get("step_id", ""))
        bound = RQ.load(task.requirements, objective=task.objective) if task else None
        if (receipt.get("schema") != 1 or not task or receipt.get("task_id") != task.task_id
                or receipt.get("run_id") != run_id or receipt.get("artifact_id") != artifact_id
                or receipt.get("sha256") != artifact.sha256 or receipt.get("size") != artifact.bytes
                or receipt.get("requirements_digest") != (RQ.digest_of(bound) if bound else "")
                or step is None or step.run_id != run_id or step.state != "succeeded"
                or not {artifact_id, proof.artifact_id}.issubset(step.artifact_refs)
                or receipt.get("name") != safe_name(receipt.get("name"))
                or receipt.get("preview_kind") not in {"image", "pdf", "audio", "video", "text", "none"}):
            raise ValueError("result_receipt_binding_changed")
        content = _read_file(directory, filename, artifact.bytes, artifact.sha256,
                             limit=MAX_FILE_BYTES, include_content=include_content)
    finally:
        os.close(directory)
    descriptor = _descriptor(run_id, artifact_id, receipt["name"], receipt["mime_type"],
                             artifact.bytes, artifact.sha256, receipt["preview_kind"])
    return descriptor, content, receipt


def read_result(ledger, run_id, artifact_id) -> tuple[dict, bytes]:
    descriptor, content, _ = _verified(ledger, run_id, artifact_id)
    return descriptor, content


def may_match_filename(ledger, run_id, query: str) -> bool:
    """Cheap search prefilter, never a delivery/integrity assertion.

    Only bounded receipt metadata is read here. Every eventual search result
    still passes describe_files and its full integrity/binding verification.
    Receipt paths and names are never returned by this prefilter.
    """
    rows = ledger.artifacts_for_run(run_id)
    if any(a.kind == "document_result" for a in rows) and query in "solvio-dokument.txt":
        return True
    for artifact in rows:
        if artifact.kind != "result_file" or not re.fullmatch(r"aa-[a-f0-9]{16}", artifact.artifact_id):
            continue
        name = f"result-{artifact.artifact_id}.json"
        proof = next((a for a in rows if a.kind == "result_receipt" and a.path == _path(run_id, name)), None)
        if proof is None:
            continue
        try:
            directory = DC._directory(run_id)
            try:
                raw = _read_file(directory, name, proof.bytes, proof.sha256, limit=MAX_RECEIPT_BYTES)
            finally:
                os.close(directory)
            receipt = json.loads(raw)
            candidate = receipt.get("name") if isinstance(receipt, dict) else None
            if isinstance(candidate, str) and query in candidate.casefold():
                return True
        except (ValueError, OSError, TypeError, KeyError):
            continue
    return False


def superseded_artifact_ids(ledger, run_id) -> frozenset:
    """Artifacts of an earlier worker step that a later SUCCEEDED worker step of the
    same run replaced (task rework, N8/C4 §3.5): the rework republishes every
    deliverable of the order, so the earlier publication, observation and helper
    candidates are history — kept in the ledger, no longer a result of the run.
    One succeeded worker step supersedes nothing."""
    from solvio.agent_runtime.specialists import WORKER_PROFILES
    steps = [s for s in ledger.steps_for_run(run_id)
             if s.kind == "specialist" and s.specialist_profile in WORKER_PROFILES.values()]
    done = [s for s in steps if s.state == "succeeded"]
    if len(done) < 2:
        return frozenset()
    latest = max(done, key=lambda s: (s.seq, s.attempt))
    return frozenset(ref for s in steps if s.step_id != latest.step_id for ref in s.artifact_refs)


def describe_files(ledger, run_id) -> tuple[list[dict], str]:
    files, unavailable = [], False
    superseded = superseded_artifact_ids(ledger, run_id)
    for artifact in ledger.artifacts_for_run(run_id):
        if artifact.kind not in DELIVERABLE_KINDS or artifact.artifact_id in superseded:
            continue
        try:
            descriptor, _, _ = _verified(ledger, run_id, artifact.artifact_id, include_content=False)
            files.append(descriptor)
        except (ValueError, OSError, TypeError, KeyError):
            unavailable = True
    message = "Mindestens eine Ergebnisdatei ist noch nicht bestätigt oder nicht mehr verfügbar." if unavailable else ""
    return files, message


# -- result_files_list v1: a closed local READ_ONLY DynamicTool (N8/C4 §4) ---
#
# Descriptors of this task's confirmed result files only (every run of the
# task, never a predecessor task — E4). No content, no URL, no other PII. The
# price contract is the reviewed implementation below, matched by the router's
# `_task_service_route`; a same-name handler never inherits `free_local`.
LIST_CAPABILITY = "result_files_list"
LIST_VERSION = 1
LIST_ROUTE = ("local.result-files", "describe")
LIST_CONTRACT = "solvio:result-files-list-local:v1"
LIST_FIELDS = ("name", "mime_type", "size", "sha256", "run_id", "requirement")


def _list_spec():
    from solvio.capabilities.contract import CapabilitySpec, ExecutionClass
    from solvio.security.mobile_approval.execution import READ_ONLY
    from solvio.tools.base import RiskLevel
    return CapabilitySpec(name=LIST_CAPABILITY, version=LIST_VERSION, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {}}, executor="local", timeout=15.0,
        description="Nennt die im Core bestätigten Ergebnisdateien dieses Auftrags (Name, Typ, "
                    "Größe, SHA-256, Lauf, Anforderung). Liest keine Inhalte.")


LIST_SPEC = _list_spec()


def describe_task_files(ledger, task_id) -> list[dict]:
    """Descriptors for every run of ONE task; unavailable files are omitted."""
    rows = []
    for run in ledger.runs_for_task(task_id):
        superseded = superseded_artifact_ids(ledger, run.run_id)
        for artifact in ledger.artifacts_for_run(run.run_id):
            if artifact.kind not in DELIVERABLE_KINDS or artifact.artifact_id in superseded:
                continue
            try:
                descriptor, _, receipt = _verified(ledger, run.run_id, artifact.artifact_id, include_content=False)
            except (ValueError, OSError, TypeError, KeyError):
                continue
            requirement = receipt.get("requirement", "") if receipt else ""
            rows.append({"name": descriptor["name"], "mime_type": descriptor["mime_type"],
                "size": descriptor["size"], "sha256": descriptor["sha256"], "run_id": run.run_id,
                "requirement": requirement if isinstance(requirement, str) else ""})
    return rows


@dataclass(frozen=True)
class _LocalResultFiles:
    """The registered handler carries its internal zero-cost contract.

    Bound to one ledger; the task comes from the verified TaskStepAuthority the
    router hands over after its grant check, never from model arguments.
    """
    ledger: object

    def resources(self, spec, arguments: dict, task_step=None) -> dict:
        from solvio.agent_runtime.task_start_service import TaskStepAuthority
        if (spec is not LIST_SPEC or spec.version != LIST_VERSION
                or spec.input_schema != {"type": "object", "properties": {}}
                or type(arguments) is not dict or arguments
                or type(self.ledger) is not S.AgentRunLedger
                or task_step is not None and type(task_step) is not TaskStepAuthority):
            raise ValueError("local_service_binding_invalid")
        return {"contract": LIST_CONTRACT, "ledger_path": self.ledger.path,
                "task_id": task_step.task_id if task_step is not None else "",
                "run_id": task_step.run_id if task_step is not None else ""}

    def quote(self, service, invocation):
        from solvio.agent_runtime.cost_dispatch import CostQuote
        from solvio.agent_runtime.costs import CostEvidence
        if (service != LIST_ROUTE[0] or invocation.capability != LIST_CAPABILITY
                or invocation.version != LIST_VERSION or invocation.operation != LIST_ROUTE[1]):
            raise ValueError("local_service_binding_invalid")
        return CostQuote(0, CostEvidence("free_local", LIST_CONTRACT))

    async def execute(self, arguments, task_step):
        from solvio.agent_runtime.task_start_service import TaskStepAuthority
        if type(task_step) is not TaskStepAuthority or arguments:
            raise ValueError("local_service_binding_invalid")
        return {"dateien": describe_task_files(self.ledger, task_step.task_id)}

    async def __call__(self, arguments):
        # No interactive path: descriptors belong to a bound task, never to a
        # conversational call that carries no task authority.
        from solvio.capabilities.contract import CapabilityRefused
        raise CapabilityRefused("action_task_authority_required",
                                "Diese Auskunft gehoert zu einem gebundenen Auftrag.")


_LOCAL_RESULT_FILES_SERVICE_METHODS = {name: getattr(_LocalResultFiles, name)
                                       for name in ("resources", "quote", "execute")}


def register(router, ledger) -> list[str]:
    """Register the closed local service once on a router bound to this ledger."""
    if router.spec(LIST_CAPABILITY) is not None:
        raise ValueError("capability already registered: " + LIST_CAPABILITY)
    router.register(LIST_SPEC, _LocalResultFiles(ledger))
    return [LIST_CAPABILITY]


@dataclass(frozen=True)
class FileDelivery:
    artifact_id: str
    evidence: str
    requirement: str


def completion_evidence(ledger, run_id) -> tuple[FileDelivery, ...]:
    """Recompute availability before and after assessment, also on recovery."""
    results = []
    superseded = superseded_artifact_ids(ledger, run_id)
    for artifact in ledger.artifacts_for_run(run_id):
        if artifact.kind != "result_file" or artifact.artifact_id in superseded:
            continue
        descriptor, _, receipt = _verified(ledger, run_id, artifact.artifact_id, include_content=False)
        evidence = (f"Core-Dateibeleg: {descriptor['name']} ({descriptor['mime_type']}, "
            f"{descriptor['size']} Bytes, SHA-256 {descriptor['sha256']}) wurde erzeugt und "
            f"unverändert zum Download bereitgestellt: {descriptor['download_url']}. "
            "Das bestätigt die Datei, nicht eine Nutzeransicht oder weitere inhaltliche Anforderungen.")
        producer = receipt.get("producer") or {}
        if (producer.get("runtime") == "hermes-codex-image-generation"
                and producer.get("terminal") == "completed"):
            steps = [s for s in ledger.steps_for_run(run_id) if s.kind == "specialist"]
            images = [s for s in steps if s.specialist_profile == "image/codex"]
            evidence += (" Nativer Erzeugungsbeleg: ein bestätigter Turn mit genau einem "
                "Bildereignis; Recherche und andere Werkzeuge sind in diesem Erzeugungsweg "
                f"gesperrt. Im Core-Lauf stehen {len(images)} Bildschritt(e) und "
                f"{len(steps) - len(images)} andere Spezialistenschritt(e). "
                "Der Bildinhalt ist separat anhand des angehängten Bildes zu beurteilen.")
        results.append(FileDelivery(artifact.artifact_id, evidence, receipt["requirement"]))
    return tuple(results)
