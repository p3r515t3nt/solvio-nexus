"""Task-local activation of fixed Core-owned document contracts.

The existing agent artifact ledger owns candidates; the existing Autopilot
publisher owns source commits and test evidence. This table holds only a run's
selected artifact and its exact predecessor, never a capability or authority
registry. No generated Python is imported into Core. Publication, a passing
builder test or READY alone cannot activate an implementation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time

from solvio import git_binary
from solvio.agent_runtime import store as S
from solvio.agent_runtime import extension_process as EP
from solvio.agent_runtime import extension_families as EF
from solvio.autopilot.publisher import build_ref

ENTRYPOINT = "adapter.py"
GATE_KIND = "rtf_extension_gate_v1"
# Core fixtures stay outside the builder workspace and cannot be replaced by it.
_CASES = ((b"{\\rtf1\\ansi SOLVIO bound document.}", "SOLVIO bound document."),
          (b"{\\rtf1\\ansi First\\par Second \\b bold\\b0 .}", "First\nSecond bold."))
_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_extension_selection (
 run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id),
 contract_digest TEXT NOT NULL,
 active_artifact TEXT NOT NULL DEFAULT '',
 previous_artifact TEXT NOT NULL DEFAULT '',
 pending_artifact TEXT NOT NULL DEFAULT '',
 generation INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL
);
"""


class ActivationRefused(ValueError):
    pass


@dataclass(frozen=True)
class ActivationResult:
    ok: bool
    reason: str = ""
    artifact_id: str = ""
    previous_artifact: str = ""
    rolled_back: bool = False


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def gate_cases(input_format="rtf"):
    if input_format == "rtf":
        return _CASES
    from solvio.agent_runtime import document_formats as DF
    return DF.gate_cases(input_format)


def gate_kind(input_format="rtf"):
    return GATE_KIND if input_format == "rtf" else input_format + "_extension_gate_v1"


def environment_fingerprint(input_format="rtf") -> str:
    from solvio.agent_runtime import extension_versions as EV
    # Runtime and native converter are part of the actual execution contract.
    body = {"process_contract": _sha(Path(EP.__file__).read_bytes()),
        "activation_contract": _sha(Path(__file__).read_bytes()),
        "version_contract": _sha(Path(EV.__file__).read_bytes()),
        "families_contract": _sha(Path(EF.__file__).read_bytes()),
        "bootstrap": _sha(EP._BOOTSTRAP.encode()), "python": sys.version,
        "runtime": _sha(Path(os.path.realpath(sys.executable)).read_bytes()), "os": platform.platform(),
        "textutil": _sha(Path(EP.TEXTUTIL).read_bytes()),
        "cases": [(_sha(data), expected) for data, expected in gate_cases(input_format)]}
    if input_format != "rtf":
        from solvio.agent_runtime import document_formats as DF
        body.update(format_contract=DF.profile(input_format).contract_digest,
                    validator=_sha(Path(DF.__file__).read_bytes()))
    return _sha(_encoded(body))


class ExtensionActivation:
    def __init__(self, ledger, *, development, publisher, file_runtime=None):
        self.ledger, self.development, self.publisher = ledger, development, publisher
        self.file_runtime = file_runtime
        with ledger._open() as connection:
            connection.executescript(_SCHEMA)
        from solvio.agent_runtime.extension_versions import ExtensionVersions
        self.versions = ExtensionVersions(self)

    def _bound(self, run_id):
        bound = EF.bound_for_run(self.ledger, run_id, file_runtime=self.file_runtime)
        if bound is None:
            raise ActivationRefused("extension_contract_not_authorized")
        return bound

    def _publication(self, run_id, milestone_id, commit, checkpoint_id):
        run = self.ledger.get_run(run_id)
        if run is None or run.development_ref != milestone_id:
            raise ActivationRefused("development_binding_changed")
        return self._published_reference(milestone_id, commit, checkpoint_id)

    def _published_reference(self, milestone_id, commit, checkpoint_id):
        # Publication proves code provenance, never the caller's authority.
        # Reuse checks its NEW task independently and preserves this origin.
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ActivationRefused("full_commit_required")
        ref = build_ref(milestone_id, checkpoint_id)
        if (ref, commit) not in self.publisher.published(milestone_id):
            raise ActivationRefused("published_commit_required")
        evidence = self.development.fresh_evidence(milestone_id,
            kind="checkpoint_ref", commit=commit, env_fingerprint="")
        try:
            body = json.loads(evidence.payload_json) if evidence else {}
        except ValueError:
            body = {}
        if (not evidence or not evidence.ok or body.get("canonical_ref") != ref
                or body.get("commit") != commit):
            raise ActivationRefused("publication_evidence_required")
        return ref

    async def prepare_reuse(self, run_id: str, version_id: str) -> str:
        return await self.versions.prepare_for_run(run_id, version_id)

    def _source(self, commit):
        # The object name is fixed from a full commit, never shell/path input.
        env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_SYSTEM": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
        base = [git_binary.resolve(), "-C", self.publisher.canonical, "cat-file"]
        obj = commit + ":" + ENTRYPOINT
        size = subprocess.run(base + ["-s", obj], env=env, capture_output=True,
                              timeout=10, check=False)
        if size.returncode or not size.stdout.strip().isdigit():
            raise ActivationRefused("adapter_absent")
        if not 0 < int(size.stdout) <= EP.MAX_ARTIFACT_BYTES:
            raise ActivationRefused("adapter_size")
        source = subprocess.run(base + ["blob", obj], env=env, capture_output=True,
                                timeout=10, check=False)
        if source.returncode or len(source.stdout) != int(size.stdout):
            raise ActivationRefused("adapter_unreadable")
        return source.stdout

    async def _gate(self, invocation, input_format="rtf"):
        for source, expected in gate_cases(input_format):
            if input_format != "rtf":
                from solvio.agent_runtime.document_formats import validate
                validate(source, input_format)
            result = await EP.run_extension(invocation, source)
            try:
                actual = result.stdout.decode("utf-8").rstrip("\n")
            except UnicodeDecodeError:
                return False
            if not result.ok or result.execution_status != "terminal" or actual != expected:
                return False
        return True

    async def _gate_for(self, invocation, input_format):
        # Existing RTF gate hooks and fixtures keep their original call shape.
        if input_format not in {"rtf", "txt", "docx", "odt"}:
            return await EF.file_gate(invocation, input_format, file_runtime=self.file_runtime)
        return await (self._gate(invocation) if input_format == "rtf"
                      else self._gate(invocation, input_format))

    async def prepare(self, run_id: str, *, milestone_id: str, commit: str,
                      checkpoint_id: str) -> str:
        from solvio.agent_runtime import document_contract as DC
        bound = self._bound(run_id)
        ref = self._publication(run_id, milestone_id, commit, checkpoint_id)
        source = self._source(commit)
        environment = EF.environment_fingerprint(bound.format, file_runtime=self.file_runtime)
        payload = {"v": 1, "run_id": run_id, "contract_digest": bound.contract_digest,
            "arguments": bound.arguments, "milestone_id": milestone_id,
            "commit": commit, "checkpoint_id": checkpoint_id, "canonical_ref": ref,
            "environment": environment, "entrypoint": ENTRYPOINT,
            "files": {ENTRYPOINT: _sha(source)}}
        candidate_digest = _sha(_encoded(payload))
        root = Path(S.artifact_root(run_id)).resolve() / ("extension-" + candidate_digest)
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The directory is an immutable snapshot. Refuse links or changed files
        # on reuse rather than overwriting evidence from an earlier candidate.
        if root.exists():
            if root.is_symlink() or root.resolve() != root:
                raise ActivationRefused("candidate_path_changed")
            if (root / ENTRYPOINT).read_bytes() != source:
                raise ActivationRefused("candidate_source_changed")
        else:
            with tempfile.TemporaryDirectory(prefix=".candidate-", dir=root.parent) as stage:
                temp = Path(stage)
                (temp / ENTRYPOINT).write_bytes(source)
                (temp / ENTRYPOINT).chmod(0o400)
                # Rename only into an absent destination; another preparation
                # is reconciled by the content binding below.
                try:
                    os.rename(temp, root)
                except FileExistsError:
                    if (root / ENTRYPOINT).read_bytes() != source:
                        raise ActivationRefused("candidate_source_changed") from None
        invocation = EF.invocation(bound, root, ENTRYPOINT, payload["files"], file_runtime=self.file_runtime)
        ok = await self._gate_for(invocation, bound.format)
        # Cancellation/revocation or publication changes during testing prevent
        # activation. Gate records are Core-generated, never builder claims.
        if self._bound(run_id).arguments != bound.arguments:
            raise ActivationRefused("document_binding_changed")
        self._publication(run_id, milestone_id, commit, checkpoint_id)
        evidence_id = self.development.record_evidence(milestone_id, kind=gate_kind(bound.format),
            commit=commit, env_fingerprint=environment, ok=ok,
            summary=f"Core {bound.format.upper()} contract probes " + ("passed." if ok else "failed."),
            payload={"candidate_digest": candidate_digest, "run_id": run_id,
                     "contract_digest": bound.contract_digest})
        if not ok:
            raise ActivationRefused("extension_gate_failed")
        manifest = dict(payload, candidate_digest=candidate_digest, gate_evidence=evidence_id)
        data = _encoded(manifest)
        # Each measured gate gets its own immutable manifest (including repeats).
        path = root / (evidence_id + ".json")
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o400)
        artifact = self.ledger.add_artifact(run_id=run_id, kind="extension_candidate",
            path=str(path), sha256=_sha(data), size=len(data))
        return artifact.artifact_id

    def _latest_gate(self, body):
        from solvio.agent_runtime import document_contract as DC
        selected = EF.profile_for_digest(body["contract_digest"], file_runtime=self.file_runtime)
        rows = self.development._db.execute(
            "SELECT evidence_id,payload_json FROM evidence WHERE milestone_id=? AND kind=? "
            "AND commit_sha=? AND env_fingerprint=? ORDER BY measured_at DESC,evidence_id DESC",
            (body["milestone_id"], gate_kind(selected.format), body["commit"], body["environment"])).fetchall()
        for row in rows:
            try:
                binding = json.loads(row["payload_json"])
            except ValueError:
                continue
            if (binding.get("candidate_digest") == body["candidate_digest"]
                    and binding.get("run_id") == body["run_id"]
                    and binding.get("contract_digest") == body["contract_digest"]):
                return self.development.evidence(row["evidence_id"])
        return None

    def candidate(self, run_id: str, artifact_id: str):
        from solvio.agent_runtime import document_contract as DC
        bound = self._bound(run_id)
        artifact = next((a for a in self.ledger.artifacts_for_run(run_id)
                         if a.artifact_id == artifact_id and a.kind == "extension_candidate"), None)
        if artifact is None:
            raise ActivationRefused("candidate_absent")
        path = Path(artifact.path)
        root = Path(S.artifact_root(run_id)).resolve()
        if (path.resolve() != path or not path.is_relative_to(root) or not path.is_file()
                or path.stat().st_size != artifact.bytes or artifact.bytes > 8192):
            raise ActivationRefused("candidate_path_changed")
        data = path.read_bytes()
        if _sha(data) != artifact.sha256:
            raise ActivationRefused("candidate_manifest_changed")
        try:
            body = json.loads(data)
            if body.get("v") == 2:
                return self.versions.validate_candidate(run_id, artifact, body)
            evidence = self.development.evidence(body["gate_evidence"])
            latest = self._latest_gate(body)
            unsigned = {k: v for k, v in body.items() if k not in ("candidate_digest", "gate_evidence")}
            if (body["v"] != 1 or body["run_id"] != run_id
                    or body["contract_digest"] != bound.contract_digest
                    or body["arguments"] != bound.arguments
                    or body["candidate_digest"] != _sha(_encoded(unsigned))
                    or body["environment"] != EF.environment_fingerprint(bound.format, file_runtime=self.file_runtime)
                    or body["entrypoint"] != ENTRYPOINT
                    or set(body["files"]) != {ENTRYPOINT}
                    or not latest or not latest.ok
                    or not evidence or not evidence.ok or evidence.kind != gate_kind(bound.format)
                    or evidence.milestone_id != body["milestone_id"]
                    or evidence.commit != body["commit"]
                    or evidence.env_fingerprint != body["environment"]
                    or json.loads(evidence.payload_json) != {
                        "candidate_digest": body["candidate_digest"], "run_id": run_id,
                        "contract_digest": bound.contract_digest}):
                raise ActivationRefused("candidate_binding_changed")
            self._publication(run_id, body["milestone_id"], body["commit"], body["checkpoint_id"])
            # Runtime separately snapshots with NOFOLLOW; this check also keeps
            # a broken candidate from being presented as available.
            source = (path.parent / ENTRYPOINT).read_bytes()
            if _sha(source) != body["files"][ENTRYPOINT]:
                raise ActivationRefused("candidate_source_changed")
            self.versions.validate_exported_candidate(artifact_id)
            return EF.invocation(bound, path.parent, ENTRYPOINT, body["files"], file_runtime=self.file_runtime)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ActivationRefused):
                raise
            raise ActivationRefused("candidate_unreadable") from None

    def selected(self, run_id: str):
        with self.ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_extension_selection WHERE run_id=?",
                                     (run_id,)).fetchone()
        if not row or row["pending_artifact"] or not row["active_artifact"]:
            return None
        try:
            return row["active_artifact"], self.candidate(run_id, row["active_artifact"])
        except (ValueError, OSError):
            return None

    async def _rollback(self, run_id, pending, previous, generation):
        with self.ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_extension_selection WHERE run_id=?",
                                     (run_id,)).fetchone()
        if (not row or row["pending_artifact"] != pending or row["generation"] != generation
                or row["previous_artifact"] != previous):
            return False
        # Availability remains hidden until the exact former version is
        # actually usable again. Restoring a pointer is not a working rollback.
        restored = ""
        if previous:
            try:
                invocation = self.candidate(run_id, previous)
                probe_ok = await self._gate_for(invocation, self._bound(run_id).format)
                self.versions.record_activation_probe(run_id, previous, probe_ok)
                if probe_ok:
                    self.candidate(run_id, previous)
                    restored = previous
            except (ValueError, OSError):
                pass
        with self.ledger._open() as connection:
            changed = connection.execute("UPDATE agent_extension_selection SET active_artifact=?,"
                "pending_artifact='',updated_at=? WHERE run_id=? AND pending_artifact=? AND generation=?",
                (restored, time.time(), run_id, pending, generation)).rowcount
        return bool(changed and restored)

    async def _drain_rollback(self, run_id, pending, previous, generation):
        rollback = asyncio.create_task(self._rollback(run_id, pending, previous, generation))
        cancelled = False
        while not rollback.done():
            try:
                await asyncio.shield(rollback)
            except asyncio.CancelledError:
                cancelled = True
        result = rollback.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def recover(self, run_id: str) -> bool:
        with self.ledger._open() as connection:
            row = connection.execute("SELECT * FROM agent_extension_selection WHERE run_id=?",
                                     (run_id,)).fetchone()
        if not row or not row["pending_artifact"]:
            return False
        return await self._drain_rollback(run_id, row["pending_artifact"],
                                          row["previous_artifact"], row["generation"])

    async def activate(self, run_id: str, artifact_id: str) -> ActivationResult:
        from solvio.agent_runtime import document_contract as DC
        bound = self._bound(run_id)
        invocation = self.candidate(run_id, artifact_id)
        with self.ledger._open() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM agent_extension_selection WHERE run_id=?",
                                     (run_id,)).fetchone()
            if row and row["pending_artifact"]:
                return ActivationResult(False, "activation_in_progress", artifact_id)
            if row and row["active_artifact"] == artifact_id:
                return ActivationResult(True, artifact_id=artifact_id,
                                        previous_artifact=row["previous_artifact"])
            previous = row["active_artifact"] if row else ""
            generation = int(row["generation"]) + 1 if row else 1
            connection.execute("INSERT INTO agent_extension_selection VALUES (?,?, '',?,?,1,?) "
                "ON CONFLICT(run_id) DO UPDATE SET previous_artifact=excluded.previous_artifact,"
                "pending_artifact=excluded.pending_artifact,generation=generation+1,updated_at=excluded.updated_at",
                (run_id, bound.contract_digest, previous, artifact_id, time.time()))
        completed = False
        try:
            probe_ok = await self._gate_for(invocation, bound.format)
            self.versions.record_activation_probe(run_id, artifact_id, probe_ok)
            if not probe_ok:
                restored = await self._drain_rollback(run_id, artifact_id, previous, generation)
                return ActivationResult(False, "activation_probe_failed", artifact_id, previous,
                                        rolled_back=restored)
            self.candidate(run_id, artifact_id)
            with self.ledger._open() as connection:
                changed = connection.execute("UPDATE agent_extension_selection SET active_artifact=?,"
                    "pending_artifact='',updated_at=? WHERE run_id=? AND pending_artifact=? AND generation=?",
                    (artifact_id, time.time(), run_id, artifact_id, generation)).rowcount
            completed = bool(changed)
            if not completed:
                return ActivationResult(False, "activation_changed", artifact_id, previous)
            self.ledger.record_event(run_id, "state_changed", "Gepruefter Dokumentadapter bereit.",
                                     ref=artifact_id)
            return ActivationResult(True, artifact_id=artifact_id, previous_artifact=previous)
        finally:
            if not completed:
                await self._drain_rollback(run_id, artifact_id, previous, generation)
