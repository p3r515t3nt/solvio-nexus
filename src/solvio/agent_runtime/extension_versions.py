"""Reusable code provenance, with a fresh task-local activation for every use.

Rows share the AgentRunLedger. They are not capabilities or grants. A completed
origin task supplies historical publication/gate evidence only; its authority
is never checked as a way of authorizing the next task. No generated code is
imported, and no provider is called here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from solvio.agent_runtime import extension_families as EF, extension_process as EP
from solvio.agent_runtime import store as S

# N8/C4 §3: reusable native helpers share these three tables as a second
# family. No cage contract, no loader, no import: the Core seeds read-only
# copies into a NEW task's workspace and rechecks every tool call there.
HELPER_FAMILY = "native_helper"
HELPER_CONTRACT = "solvio:native-helper:v1"
HELPER_CANDIDATE_KIND = "helper_candidate"
HELPER_EVENT = "helper_published"
MAX_HELPER_VERSIONS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_extension_versions (
 version_id TEXT PRIMARY KEY,
 owner TEXT NOT NULL,
 family TEXT NOT NULL,
 contract_digest TEXT NOT NULL,
 environment TEXT NOT NULL,
 provenance TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_extension_version_probes (
 probe_id INTEGER PRIMARY KEY AUTOINCREMENT,
 version_id TEXT NOT NULL REFERENCES agent_extension_versions(version_id),
 run_id TEXT NOT NULL,
 candidate_digest TEXT NOT NULL,
 ok INTEGER NOT NULL CHECK(ok IN (0,1)),
 evidence_id TEXT NOT NULL,
 evidence_digest TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_extension_version_revocations (
 version_id TEXT PRIMARY KEY REFERENCES agent_extension_versions(version_id),
 reason TEXT NOT NULL,
 created_at REAL NOT NULL
);
"""


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _evidence_digest(evidence):
    return _sha(_encode(asdict(evidence)))


def _refuse(reason):
    from solvio.agent_runtime.extension_activation import ActivationRefused
    raise ActivationRefused(reason)


def _immutable(path, *, size, digest, limit):
    """Read an exact regular immutable artifact, not a link or changed inode."""
    path = Path(path)
    if path.resolve() != path or not 0 < size <= limit:
        _refuse("version_artifact_path_changed")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o277 or before.st_size != size):
            _refuse("version_artifact_changed")
        data = os.read(fd, size + 1)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mode,
                               st.st_nlink, st.st_mtime_ns, st.st_ctime_ns)
        if (identity(before) != identity(after) or identity(named) != identity(before)
                or len(data) != size or _sha(data) != digest):
            _refuse("version_artifact_changed")
        return data
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ExtensionVersion:
    version_id: str
    owner: str
    family: str
    contract_digest: str
    environment: str
    commit: str
    source_sha256: str
    origin_run_id: str
    origin_artifact_id: str


@dataclass(frozen=True)
class HelperVersion:
    """Metadata plus the exact validated bytes; never authority for a new task."""
    version_id: str
    owner: str
    name: str
    purpose: str
    files: dict          # {file name: sha256}
    contents: dict       # {file name: bytes}
    origin_run_id: str
    origin_artifact_id: str


def helper_contract_digest():
    return _sha(HELPER_CONTRACT.encode("utf-8"))


def helper_environment():
    """The static checker IS the helper environment; editing it retires versions."""
    from solvio.agent_runtime import helper_check as HC
    return _sha(_encode({"helper_check": _sha(Path(HC.__file__).read_bytes())}))


def helper_provenance(owner, digests):
    """Content-addressed: no run/task/artifact identifiers, so identical bytes
    from later runs map onto the same version (INSERT OR IGNORE, no duplicate)."""
    if (type(owner) is not str or not owner or type(digests) is not dict or not digests
            or any(type(k) is not str or type(v) is not str or not re.fullmatch(r"[0-9a-f]{64}", v)
                   for k, v in digests.items())):
        _refuse("helper_provenance_invalid")
    return {"v": 1, "family": HELPER_FAMILY, "owner": owner,
            "contract_digest": helper_contract_digest(), "environment": helper_environment(),
            "files": dict(sorted(digests.items()))}


def helper_version_id(owner, digests):
    return "extension-v1-" + _sha(_encode(helper_provenance(owner, digests)))


class _LedgerOnly:
    """Helper-family access needs the ledger only; adapter paths stay unavailable."""

    def __init__(self, ledger):
        self.ledger = ledger


def helper_versions(ledger):
    return ExtensionVersions(_LedgerOnly(ledger))


class ExtensionVersions:
    def __init__(self, activation):
        self.activation = activation
        self.ledger = activation.ledger
        with self.ledger._open() as connection:
            connection.executescript(SCHEMA)

    def _owner(self, run_id):
        run = self.ledger.get_run(run_id)
        task = self.ledger.get_task(run.task_id) if run else None
        if not task or not task.created_principal:
            _refuse("version_owner_absent")
        return task.created_principal

    def _artifact(self, run_id, artifact_id):
        artifact = next((a for a in self.ledger.artifacts_for_run(run_id)
                         if a.artifact_id == artifact_id and a.kind == "extension_candidate"), None)
        if artifact is None:
            _refuse("version_origin_absent")
        path = Path(artifact.path)
        if not path.is_relative_to(Path(S.artifact_root(run_id)).resolve()):
            _refuse("version_artifact_path_changed")
        raw = _immutable(path, size=artifact.bytes, digest=artifact.sha256, limit=8192)
        try:
            body = json.loads(raw)
            if type(body) is not dict:
                raise ValueError()
        except (ValueError, TypeError):
            _refuse("version_manifest_invalid")
        return artifact, body

    def publish(self, run_id: str, artifact_id: str) -> str:
        """Publish only code already selected under a current genuine grant."""
        bound = self.activation._bound(run_id)
        selected = self.activation.selected(run_id)
        if selected is None or selected[0] != artifact_id:
            _refuse("version_active_candidate_required")
        artifact, body = self._artifact(run_id, artifact_id)
        if body.get("v") == 2:
            # Reuse does not invent another lineage for the same code.
            self.validate_candidate(run_id, artifact, body)
            return body["version_id"]
        profile = EF.profile_for_digest(bound.contract_digest, file_runtime=self.activation.file_runtime)
        original_gate = self.activation.development.evidence(body["gate_evidence"])
        publication = self.activation.development.fresh_evidence(body["milestone_id"],
            kind="checkpoint_ref", commit=body["commit"], env_fingerprint="")
        provenance = {"v": 1, "family": profile.family, "owner": self._owner(run_id),
            "contract_digest": bound.contract_digest,
            "environment": EF.environment_fingerprint(bound.format, file_runtime=self.activation.file_runtime),
            "origin_run_id": run_id, "origin_artifact_id": artifact_id,
            "origin_manifest_sha256": artifact.sha256, "origin_gate_id": original_gate.evidence_id,
            "origin_gate_digest": _evidence_digest(original_gate),
            "publication_id": publication.evidence_id, "publication_digest": _evidence_digest(publication),
            "publisher": str(Path(self.activation.publisher.canonical).resolve()),
            "commit": body["commit"], "source_sha256": body["files"]["adapter.py"]}
        encoded = _encode(provenance).decode("utf-8")
        version_id = "extension-v1-" + _sha(encoded.encode("utf-8"))
        # Both reads are pure validation. The row conveys no authority, even
        # if its origin is cancelled immediately after this insertion.
        self.activation.candidate(run_id, artifact_id)
        with self.ledger._open() as connection:
            connection.execute("INSERT OR IGNORE INTO agent_extension_versions VALUES (?,?,?,?,?,?,?)",
                (version_id, provenance["owner"], profile.family, bound.contract_digest,
                 provenance["environment"], encoded, time.time()))
        self.get(version_id)
        return version_id

    def _provenance(self, version_id):
        if not isinstance(version_id, str) or not re.fullmatch(r"extension-v1-[0-9a-f]{64}", version_id):
            _refuse("version_id_invalid")
        with self.ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_extension_versions WHERE version_id=?",
                                     (version_id,)).fetchone()
            blocked = connection.execute("SELECT 1 FROM agent_extension_version_revocations WHERE version_id=? "
                "UNION ALL SELECT 1 FROM agent_extension_version_probes WHERE version_id=? AND ok=0 LIMIT 1",
                (version_id, version_id)).fetchone()
        if row is None or blocked:
            _refuse("version_unavailable")
        try:
            data = json.loads(row["provenance"])
            if (version_id != "extension-v1-" + _sha(_encode(data)) or data["v"] != 1
                    or any(row[k] != data[k] for k in ("owner", "family", "contract_digest", "environment"))):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            _refuse("version_provenance_changed")
        return data

    def _validated(self, version_id):
        """Verify historical evidence without asking the old task for rights."""
        p = self._provenance(version_id)
        profile = EF.profile_for_digest(p["contract_digest"], file_runtime=self.activation.file_runtime)
        if (self._owner(p["origin_run_id"]) != p["owner"]
                or p["family"] != profile.family
                or str(Path(self.activation.publisher.canonical).resolve()) != p["publisher"]
                or EF.environment_fingerprint(profile.format,
                    file_runtime=self.activation.file_runtime) != p["environment"]):
            _refuse("version_environment_or_owner_changed")
        artifact, body = self._artifact(p["origin_run_id"], p["origin_artifact_id"])
        if (artifact.sha256 != p["origin_manifest_sha256"] or body.get("v") != 1
                or body["run_id"] != p["origin_run_id"] or body["contract_digest"] != p["contract_digest"]
                or body["environment"] != p["environment"] or body["commit"] != p["commit"]
                or body["files"] != {"adapter.py": p["source_sha256"]}
                or body["gate_evidence"] != p["origin_gate_id"]):
            _refuse("version_origin_changed")
        gate = self.activation.development.evidence(p["origin_gate_id"])
        latest = self.activation._latest_gate(body)
        publication = self.activation.development.evidence(p["publication_id"])
        if (not gate or not gate.ok or _evidence_digest(gate) != p["origin_gate_digest"]
                or not latest or not latest.ok or not publication or not publication.ok
                or _evidence_digest(publication) != p["publication_digest"]):
            _refuse("version_evidence_changed")
        with self.ledger._open() as connection:
            probes = connection.execute("SELECT * FROM agent_extension_version_probes WHERE version_id=?",
                                        (version_id,)).fetchall()
        for probe in probes:
            measured = self.activation.development.evidence(probe["evidence_id"])
            if (not measured or not measured.ok or not probe["ok"]
                    or _evidence_digest(measured) != probe["evidence_digest"]
                    or measured.commit != p["commit"] or measured.env_fingerprint != p["environment"]):
                _refuse("version_probe_changed")
        self.activation._published_reference(body["milestone_id"], body["commit"], body["checkpoint_id"])
        source = self.activation._source(body["commit"])
        if _sha(source) != p["source_sha256"]:
            _refuse("version_source_changed")
        _immutable(Path(artifact.path).parent / "adapter.py", size=len(source),
                   digest=p["source_sha256"], limit=EP.MAX_ARTIFACT_BYTES)
        return p, body, source

    def get(self, version_id: str) -> ExtensionVersion:
        p, _, _ = self._validated(version_id)
        return ExtensionVersion(version_id=version_id, **{key: p[key] for key in (
            "owner", "family", "contract_digest", "environment", "commit", "source_sha256",
            "origin_run_id", "origin_artifact_id")})

    def _target(self, run_id, version_id):
        bound = self.activation._bound(run_id)  # Exact NEW grant + input bytes.
        p, origin, source = self._validated(version_id)
        if self._owner(run_id) != p["owner"] or bound.contract_digest != p["contract_digest"]:
            _refuse("version_task_binding_changed")
        return bound, p, origin, source

    def compatible(self, run_id: str) -> tuple[ExtensionVersion, ...]:
        bound = self.activation._bound(run_id)
        profile = EF.profile_for_digest(bound.contract_digest, file_runtime=self.activation.file_runtime)
        with self.ledger._open() as connection:
            rows = connection.execute("SELECT version_id FROM agent_extension_versions "
                "WHERE owner=? AND family=? AND contract_digest=? ORDER BY created_at,version_id",
                (self._owner(run_id), profile.family, bound.contract_digest)).fetchall()
        result = []
        for row in rows:
            try:
                result.append(self.get(row["version_id"]))
            except (ValueError, OSError):
                continue
        # The caller receives code metadata, not authority. Still refuse a
        # revoked target rather than advertising actionable stale matches.
        if self.activation._bound(run_id) != bound:
            _refuse("version_task_binding_changed")
        return tuple(result)

    def revoke(self, version_id: str, reason: str):
        """Core-owned withdrawal. Historical rows and receipts remain intact."""
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            _refuse("version_revocation_reason_invalid")
        self._provenance(version_id)
        with self.ledger._open() as connection:
            connection.execute("INSERT OR IGNORE INTO agent_extension_version_revocations VALUES (?,?,?)",
                               (version_id, reason, time.time()))

    def _probe(self, body, ok, evidence):
        with self.ledger._open() as connection:
            connection.execute("INSERT INTO agent_extension_version_probes "
                "(version_id,run_id,candidate_digest,ok,evidence_id,evidence_digest,created_at) VALUES (?,?,?,?,?,?,?)",
                (body["version_id"], body["run_id"], body["candidate_digest"], int(ok),
                 evidence.evidence_id, _evidence_digest(evidence), time.time()))

    async def prepare_for_run(self, run_id: str, version_id: str) -> str:
        from solvio.agent_runtime.extension_activation import gate_kind
        bound, p, origin, source = self._target(run_id, version_id)
        payload = {"v": 2, "version_id": version_id, "run_id": run_id,
            "grant_reference": bound.grant_reference, "contract_digest": bound.contract_digest,
            "arguments": bound.arguments, "milestone_id": origin["milestone_id"],
            "commit": p["commit"], "checkpoint_id": origin["checkpoint_id"],
            "canonical_ref": origin["canonical_ref"], "environment": p["environment"],
            "entrypoint": "adapter.py", "files": {"adapter.py": p["source_sha256"]}}
        digest = _sha(_encode(payload))
        root = Path(S.artifact_root(run_id)).resolve() / ("extension-" + digest)
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.exists():
            with tempfile.TemporaryDirectory(prefix=".reuse-", dir=root.parent) as folder:
                stage = Path(folder)
                path = stage / "adapter.py"
                with path.open("xb") as stream:
                    stream.write(source)
                    stream.flush()
                    os.fsync(stream.fileno())
                path.chmod(0o400)
                try:
                    os.rename(stage, root)
                except FileExistsError:
                    pass
        _immutable(root / "adapter.py", size=len(source), digest=p["source_sha256"], limit=EP.MAX_ARTIFACT_BYTES)
        invocation = EF.invocation(bound, root, "adapter.py", payload["files"],
                                   file_runtime=self.activation.file_runtime)
        ok = await self.activation._gate_for(invocation, bound.format)
        if self._target(run_id, version_id)[0] != bound:
            _refuse("version_task_binding_changed")
        evidence_id = self.activation.development.record_evidence(origin["milestone_id"],
            kind=gate_kind(bound.format), commit=p["commit"], env_fingerprint=p["environment"], ok=ok,
            summary="Core task-local reused adapter probes " + ("passed." if ok else "failed."),
            payload={"candidate_digest": digest, "run_id": run_id, "contract_digest": bound.contract_digest})
        body = dict(payload, candidate_digest=digest, gate_evidence=evidence_id)
        self._probe(body, ok, self.activation.development.evidence(evidence_id))
        if not ok:
            _refuse("extension_gate_failed")
        raw = _encode(body)
        path = root / (evidence_id + ".json")
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o400)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        artifact = self.ledger.add_artifact(run_id=run_id, kind="extension_candidate",
            path=str(path), sha256=_sha(raw), size=len(raw))
        return artifact.artifact_id

    def validate_candidate(self, run_id, artifact, body):
        from solvio.agent_runtime.extension_activation import gate_kind
        bound, p, origin, source = self._target(run_id, body["version_id"])
        unsigned = {k: v for k, v in body.items() if k not in ("candidate_digest", "gate_evidence")}
        expected = {"v": 2, "version_id": body["version_id"], "run_id": run_id,
            "grant_reference": bound.grant_reference, "contract_digest": bound.contract_digest,
            "arguments": bound.arguments, "milestone_id": origin["milestone_id"],
            "commit": p["commit"], "checkpoint_id": origin["checkpoint_id"],
            "canonical_ref": origin["canonical_ref"], "environment": p["environment"],
            "entrypoint": "adapter.py", "files": {"adapter.py": p["source_sha256"]}}
        if unsigned != expected or body["candidate_digest"] != _sha(_encode(expected)):
            _refuse("reused_candidate_binding_changed")
        _, fresh = self._artifact(run_id, artifact.artifact_id)
        if fresh != body:
            _refuse("reused_candidate_binding_changed")
        evidence = self.activation.development.evidence(body["gate_evidence"])
        latest = self.activation._latest_gate(body)
        if (not evidence or not evidence.ok or not latest or not latest.ok
                or evidence.kind != gate_kind(bound.format)
                or evidence.milestone_id != origin["milestone_id"] or evidence.commit != p["commit"]
                or evidence.env_fingerprint != p["environment"]
                or json.loads(evidence.payload_json) != {"candidate_digest": body["candidate_digest"],
                    "run_id": run_id, "contract_digest": bound.contract_digest}):
            _refuse("reused_candidate_evidence_changed")
        with self.ledger._open() as connection:
            probe = connection.execute("SELECT * FROM agent_extension_version_probes WHERE "
                "version_id=? AND run_id=? AND candidate_digest=? AND evidence_id=? AND ok=1",
                (body["version_id"], run_id, body["candidate_digest"], body["gate_evidence"])).fetchone()
        if probe is None or probe["evidence_digest"] != _evidence_digest(evidence):
            _refuse("reused_candidate_probe_changed")
        _immutable(Path(artifact.path).parent / "adapter.py", size=len(source),
                   digest=p["source_sha256"], limit=EP.MAX_ARTIFACT_BYTES)
        return EF.invocation(bound, Path(artifact.path).parent, "adapter.py", expected["files"],
                             file_runtime=self.activation.file_runtime)

    # -- Family native_helper (N8/C4 §3.3) ----------------------------------

    def _helper_candidate(self, run_id, artifact_id):
        from solvio.agent_runtime import artifact_creation as A, helper_check as HC
        artifact = next((a for a in self.ledger.artifacts_for_run(run_id)
                         if a.artifact_id == artifact_id and a.kind == HELPER_CANDIDATE_KIND), None)
        if artifact is None:
            _refuse("helper_candidate_absent")
        path = Path(artifact.path)
        if not path.is_relative_to(Path(S.artifact_root(run_id)).resolve()):
            _refuse("version_artifact_path_changed")
        raw = _immutable(path, size=artifact.bytes, digest=artifact.sha256, limit=A.MAX_INPUT_BYTES)
        try:
            body = json.loads(raw)
            if (type(body) is not dict or body.get("v") != 3 or body.get("kind") != HELPER_CANDIDATE_KIND
                    or body.get("run_id") != run_id or type(body.get("files")) is not dict
                    or not 1 <= len(body["files"]) <= HC.MAX_FILES
                    or type(body.get("static_check")) is not dict or type(body.get("compile_probe")) is not dict
                    or type(body.get("version_id")) is not str or type(body.get("name")) is not str
                    or type(body.get("purpose")) is not str or type(body.get("step_id")) is not str):
                raise ValueError()
        except (ValueError, TypeError):
            _refuse("helper_candidate_invalid")
        return artifact, body

    def _helper_files(self, body):
        files, digests = {}, {}
        for name, item in body["files"].items():
            if (type(item) is not dict or set(item) != {"text", "sha256", "size"}
                    or type(item["text"]) is not str):
                _refuse("helper_candidate_invalid")
            data = item["text"].encode("utf-8")
            if len(data) != item["size"] or _sha(data) != item["sha256"]:
                _refuse("helper_candidate_invalid")
            files[name], digests[name] = data, item["sha256"]
        return files, digests

    def _helper_checked(self, body, files):
        """The static verdict is recomputed, never trusted from the manifest."""
        from solvio.agent_runtime import helper_check as HC
        check = HC.check_files(files)
        if not check["ok"] or check != body["static_check"]:
            _refuse("helper_static_check_failed")
        if any(name.lower().endswith(".py") for name in files) and body["compile_probe"].get("ok") is not True:
            _refuse("helper_compile_failed")
        return check

    def _helper_readback(self, run_id, body, files):
        """Core condition: a complete, unredacted native command receipt names the
        helper path and shows its bytes (cat) or its SHA-256 hex (shasum)."""
        from solvio.agent_runtime import artifact_creation as A
        from solvio.agent_runtime.native_observations import KIND, MAX_OBSERVATION_BYTES
        step = self.ledger.get_step(body["step_id"])
        if step is None or step.run_id != run_id:
            _refuse("helper_readback_missing")
        try:
            _, raw = A._read(self.ledger, run_id, step.step_id, KIND,
                             "native-turn-" + step.step_id + ".json", MAX_OBSERVATION_BYTES)
            receipts = json.loads(raw)["receipts"]
        except (ValueError, OSError, KeyError, TypeError):
            _refuse("helper_readback_missing")
        path = body.get("path", "")
        for name, data in files.items():
            digest = _sha(data)
            text = data.decode("utf-8")
            seen = False
            for receipt in receipts if type(receipts) is list else ():
                if (type(receipt) is not dict or receipt.get("kind") != "commandExecution"
                        or receipt.get("status") != "completed"):
                    continue
                command, output = receipt.get("command"), receipt.get("output")
                if any(type(block) is not dict or block.get("complete") is not True
                       or block.get("redacted") is not False or type(block.get("text")) is not str
                       for block in (command, output)):
                    continue
                named = (path and path in command["text"]) or name in command["text"]
                if named and (output["text"] == text or digest in output["text"]):
                    seen = True
                    break
            if not seen:
                _refuse("helper_readback_missing")

    def publish_helper(self, run_id: str, artifact_id: str) -> str:
        """Second publish path: from a SUCCEEDED origin run with Core readback."""
        run = self.ledger.get_run(run_id)
        if run is None or run.state != S.SUCCEEDED:
            _refuse("helper_origin_not_succeeded")
        owner = self._owner(run_id)
        artifact, body = self._helper_candidate(run_id, artifact_id)
        if body.get("task_id") != run.task_id:
            _refuse("helper_candidate_invalid")
        files, digests = self._helper_files(body)
        self._helper_checked(body, files)
        self._helper_readback(run_id, body, files)
        provenance = helper_provenance(owner, digests)
        version_id = "extension-v1-" + _sha(_encode(provenance))
        if version_id != body["version_id"]:
            _refuse("helper_environment_changed")
        with self.ledger._open() as connection:
            connection.execute("INSERT OR IGNORE INTO agent_extension_versions VALUES (?,?,?,?,?,?,?)",
                (version_id, owner, HELPER_FAMILY, provenance["contract_digest"],
                 provenance["environment"], _encode(provenance).decode("utf-8"), time.time()))
        self._validated_helper(version_id)
        kind = HELPER_EVENT if HELPER_EVENT in S.EVENT_KINDS else "state_changed"
        self.ledger.record_event(run_id, kind, "Helfer '" + body["name"] + "' veröffentlicht: "
            + version_id + " (Familie " + HELPER_FAMILY + ", Kandidat " + artifact_id
            + ", Anbieter " + str(body.get("provider", "")) + ").", step_id=body["step_id"], ref=version_id)
        return version_id

    def publish_helpers(self, run_id: str) -> list[dict]:
        """Every candidate of a run (a rework's candidates supersede the earlier
        step's); refusals are recorded, never raised past here."""
        from solvio.agent_runtime import result_files as RF
        results = []
        superseded = RF.superseded_artifact_ids(self.ledger, run_id)
        for artifact in self.ledger.artifacts_for_run(run_id):
            if artifact.kind != HELPER_CANDIDATE_KIND or artifact.artifact_id in superseded:
                continue
            try:
                results.append({"artifact_id": artifact.artifact_id,
                                "version_id": self.publish_helper(run_id, artifact.artifact_id), "reason": ""})
            except (ValueError, OSError) as exc:
                reason = str(exc) if re.fullmatch(r"[a-z_]{1,80}", str(exc)) else "helper_publication_failed"
                results.append({"artifact_id": artifact.artifact_id, "version_id": "", "reason": reason})
                kind = HELPER_EVENT if HELPER_EVENT in S.EVENT_KINDS else "state_changed"
                self.ledger.record_event(run_id, kind, "Helfer nicht veröffentlicht (Kandidat "
                    + artifact.artifact_id + "): " + reason + ".", ref=artifact.artifact_id)
        return results

    def _helper_origin(self, version_id, p):
        """The FIRST candidate that still proves this version: same content-
        addressed id, a SUCCEEDED origin of the same owner, unchanged bytes and
        an unchanged deterministic static verdict. A tampered or unpublished
        candidate never blocks a later identical one."""
        with self.ledger._open() as connection:
            rows = connection.execute("SELECT run_id,artifact_id FROM agent_artifacts WHERE kind=? "
                "ORDER BY created_at,artifact_id", (HELPER_CANDIDATE_KIND,)).fetchall()
        for row in rows:
            try:
                artifact, body = self._helper_candidate(row["run_id"], row["artifact_id"])
                if body["version_id"] != version_id:
                    continue
                run = self.ledger.get_run(artifact.run_id)
                if run is None or run.state != S.SUCCEEDED or self._owner(artifact.run_id) != p["owner"]:
                    continue
                files, digests = self._helper_files(body)
                if digests != p["files"] or helper_version_id(p["owner"], digests) != version_id:
                    continue
                self._helper_checked(body, files)
            except (ValueError, OSError):
                continue
            return artifact, body, files, digests
        _refuse("version_origin_absent")

    def _validated_helper(self, version_id):
        p = self._provenance(version_id)
        if (p["family"] != HELPER_FAMILY or p["contract_digest"] != helper_contract_digest()
                or p["environment"] != helper_environment()):
            _refuse("version_environment_or_owner_changed")
        artifact, body, files, digests = self._helper_origin(version_id, p)
        return HelperVersion(version_id=version_id, owner=p["owner"], name=body["name"],
            purpose=body["purpose"], files=digests, contents=files,
            origin_run_id=artifact.run_id, origin_artifact_id=artifact.artifact_id)

    def helpers_for(self, owner: str) -> tuple[HelperVersion, ...]:
        """Available helper versions of this owner, newest first, at most eight."""
        with self.ledger._open() as connection:
            rows = connection.execute("SELECT version_id FROM agent_extension_versions "
                "WHERE owner=? AND family=? ORDER BY created_at DESC,version_id",
                (owner, HELPER_FAMILY)).fetchall()
        result = []
        for row in rows:
            try:
                result.append(self._validated_helper(row["version_id"]))
            except (ValueError, OSError):
                continue
            if len(result) >= MAX_HELPER_VERSIONS:
                break
        return tuple(result)

    def _exported(self, artifact_id):
        with self.ledger._open() as connection:
            rows = connection.execute("SELECT version_id,provenance FROM agent_extension_versions").fetchall()
        return tuple(row["version_id"] for row in rows
                     if json.loads(row["provenance"]).get("origin_artifact_id") == artifact_id)

    def validate_exported_candidate(self, artifact_id):
        # Withdrawal applies to the original active snapshot as well as every
        # later consumer. This validates history, without recursive authority.
        for version_id in self._exported(artifact_id):
            self._validated(version_id)

    def record_activation_probe(self, run_id, artifact_id, ok):
        artifact, body = self._artifact(run_id, artifact_id)
        if body.get("v") == 2:
            versions = (body["version_id"],)
            self.validate_candidate(run_id, artifact, body)
        else:
            # A later failed rollback probe of an exported original snapshot
            # must also withdraw that version from already-running consumers.
            versions = self._exported(artifact_id)
            if not versions:
                return
            self.activation.candidate(run_id, artifact_id)
            for version_id in versions:
                self._validated(version_id)
        # Check exact own authority after the await, then retain the actual
        # native measurement. A negative result withdraws this code version.
        for version_id in versions:
            evidence_id = self.activation.development.record_evidence(body["milestone_id"],
                kind="extension_reuse_activation_v1", commit=body["commit"],
                env_fingerprint=body["environment"], ok=bool(ok),
                summary="Core version activation " + ("passed." if ok else "failed."),
                payload={"version_id": version_id, "run_id": run_id,
                         "candidate_digest": body["candidate_digest"]})
            self._probe(dict(body, version_id=version_id), ok,
                        self.activation.development.evidence(evidence_id))
