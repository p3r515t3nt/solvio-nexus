"""The existing Autopilot, bound to one original document task.

This service develops and publishes an adapter; it cannot activate one. Builder
and technical lead use the existing subscribed transports inside the original
task's cost/grant scope. Only Core fixtures run at the test seam. No document
content or source pathname enters either model's context.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from solvio.agent_runtime import cost_dispatch as D, document_contract as DC
from solvio.agent_runtime import extension_activation as EA, extension_process as EP
from solvio.agent_runtime import extension_families as EF
from solvio.agent_runtime import specialists as SP, workspace as W, store as S
from solvio.autopilot import builders as B, capacity as CAP, contract as C
from solvio.autopilot import driver as DR, lead as LEAD, machine as M, store as A
from solvio.specialists import providers as P
from solvio.specialists.subscription import SubscriptionTransport

SEED_README = "# SOLVIO isolated document adapter\n\nCore-owned empty development seed.\n"
WORKSPACE_EVIDENCE = "extension_workspace_v1"
PAUSE_EVIDENCE = "extension_development_pause_v1"
BLOCKERS = frozenset({"quota", "logged_out", "subscription_required", "auth_unknown",
    "auth_status_failed", "provider_unavailable", "cost_unbounded",
    "cost_approval_required", "cost_recovery_required"})


@dataclass(frozen=True)
class DevelopmentResult:
    state: str
    reason: str = ""
    artifact_id: str = ""  # Activation.prepare creates it after this service returns.
    commit: str = ""
    checkpoint_id: str = ""
    provider: str = ""
    dispatch_started: bool = False
    cost_status: str = ""
    resume_allowed: bool = False
    billing_mode: str = "subscription"
    auth: str = ""
    usage_reported: bool = False


class DevelopmentPaused(ValueError):
    def __init__(self, reason, *, dispatch_started=False, provider="codex", cost_status="",
                 billing_mode="subscription", auth="", usage_reported=False,
                 terminal_readonly_quota=False):
        super().__init__(reason)
        self.result = DevelopmentResult("paused", reason=reason, provider=provider,
            dispatch_started=dispatch_started, cost_status=cost_status,
            billing_mode=billing_mode, auth=auth, usage_reported=usage_reported,
            resume_allowed=((not dispatch_started and reason in BLOCKERS
                             and reason != "cost_recovery_required")
                            or (terminal_readonly_quota and reason == "quota")))


def seed_repository(directory: str) -> str:
    """Core startup helper: one empty local seed, never a copy of Core or input.

    The configured directory is a Core argument. Existing contents must match
    exactly; this function never resets or repairs an existing repository.
    The caller allowlists the returned path in its WorkspaceManager.
    """
    path = Path(directory).expanduser().absolute()
    if path != path.resolve():
        raise DevelopmentPaused("seed_path_changed")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    if not list(path.iterdir()):
        W._git("init", "-q", "-b", "main", cwd=str(path))
        (path / "README.md").write_text(SEED_README, encoding="utf-8")
        W._git("add", "README.md", cwd=str(path))
        W._git("-c", "user.name=SOLVIO", "-c", "user.email=local@solvio.invalid",
               "commit", "-qm", "Core document adapter seed", cwd=str(path))
    _check_seed(str(path))
    return str(path)


def _check_seed(repository):
    path = Path(repository)
    if (not path.is_absolute() or path != path.resolve() or path.is_symlink()
            or not (path / ".git").is_dir() or (path / ".git").is_symlink()
            or {p.name for p in path.iterdir()} != {".git", "README.md"}
            or (path / "README.md").is_symlink()
            or (path / "README.md").read_text(encoding="utf-8") != SEED_README):
        raise DevelopmentPaused("seed_not_empty_contract")
    W.WorkspaceManager(allowed=(str(path),))._assert_local_config(str(path))
    if (W._git("ls-tree", "-r", "--name-only", "HEAD", cwd=str(path)).stdout.strip() != "README.md"
            or W._git("status", "--porcelain", cwd=str(path)).stdout.strip()
            or W._git("remote", cwd=str(path)).stdout.strip()):
        raise DevelopmentPaused("seed_not_empty_contract")


class ExtensionDevelopment:
    def __init__(self, ledger, development, workspaces, publisher,
                 quote_adapter=None, settlement_adapter=None, file_runtime=None):
        self.ledger, self.development = ledger, development
        self.workspaces, self.publisher = workspaces, publisher
        self.quote_adapter, self.settlement_adapter = quote_adapter, settlement_adapter
        self.file_runtime = file_runtime

    def _bound(self, run_id):
        run = self.ledger.get_run(run_id)
        try:
            bound = EF.bound_for_run(self.ledger, run_id, file_runtime=self.file_runtime)
        except ValueError:
            raise DevelopmentPaused("document_contract_not_authorized") from None
        if not run or not run.development_ref or bound is None:
            raise DevelopmentPaused("document_contract_not_authorized")
        return run, bound

    def contract_for(self, run_id: str, repository: str) -> C.Contract:
        run, bound = self._bound(run_id)
        _check_seed(repository)
        if repository != self.publisher.canonical:
            raise DevelopmentPaused("publisher_repository_changed")
        if bound.format == "tables":
            from solvio.agent_runtime import table_report_contract as TC
            return C.Contract(run.development_ref, "1.0.0", TC.development_objective(),
                (C.AcceptanceCriterion("gate_green",
                    "Core independently opens actual CSV/XLSX inputs, workbook, PNG and PDF and verifies statistics.",
                    C.DETERMINISTIC),),
                non_goals=("Read original input or task-private metadata.", "Activate or register tools."),
                security_boundaries=("Only adapter.py; offline execution in measured Office Python.",
                    "No new authority, provider or monetary budget."),
                permitted_actions=("Edit adapter.py in the isolated workspace.",), repository=repository)
        if bound.format != "rtf":
            selected = DC.profile(bound.format)
            invocation = repr(['/usr/bin/textutil', *selected.native_arguments])
            return C.Contract(run.development_ref, "1.0.0",
                "Build only adapter.py for the Core document_extract_text v1 contract "
                + bound.contract_digest + f". Read {bound.format.upper()} bytes on stdin; write UTF-8 plain text "
                "on stdout. Replace the Python process with os.execv('/usr/bin/textutil', "
                + invocation + "). Reuse this native converter; implement no parser. "
                "No fork/subprocess, shell, network, imports from Core, input file or "
                "configuration. The Core validates the bound container before dispatch and tests "
                "published adapter.py under a deny-default OS sandbox with fixed format cases. "
                "Do not create or execute test scripts. "
                f"Original task {bound.task_id}; run {run_id}.",
                (C.AcceptanceCriterion("gate_green", "Core offline " + bound.format.upper()
                    + " contract gate passes.", C.DETERMINISTIC),),
                non_goals=("Read the original document or its metadata.", "Activate or register capabilities."),
                security_boundaries=("Only adapter.py is deployable; no Core/Vault/home/network access.",
                    "No new authority, model, provider or monetary budget."),
                permitted_actions=("Edit adapter.py in the isolated workspace.",), repository=repository)
        return C.Contract(run.development_ref, "1.0.0",
            "Build only adapter.py for the Core document_extract_text v1 contract "
            + DC.CONTRACT_DIGEST + ". Read RTF bytes on stdin; write UTF-8 plain text "
            "on stdout. Replace the Python process with os.execv('/usr/bin/textutil', "
            "['/usr/bin/textutil','-format','rtf','-convert','txt','-stdin','-stdout',"
            "'-encoding','UTF-8']). Reuse this native converter; implement no parser. "
            "No fork/subprocess, shell, network, imports from Core, input file or "
            "configuration. The Core tests published adapter.py under a deny-default "
            "OS sandbox with fixed RTF cases. Do not create or execute test scripts. "
            f"Original task {bound.task_id}; run {run_id}.",
            (C.AcceptanceCriterion("gate_green", "Core offline RTF contract gate passes.",
                                   C.DETERMINISTIC),),
            non_goals=("Read the original document or its metadata.", "Activate or register capabilities."),
            security_boundaries=("Only adapter.py is deployable; no Core/Vault/home/network access.",
                "No new authority, model, provider or monetary budget."),
            permitted_actions=("Edit adapter.py in the isolated workspace.",),
            repository=repository)

    def factory(self, run_id: str):
        """Return the existing Driver specialization; caller owns its run lock.

        Normal integration uses drive(), which also holds that existing lock.
        Milestone creation and original-run development_ref belong to the caller.
        """
        run, bound = self._bound(run_id)
        stone = self.development.milestone(run.development_ref)
        repository = stone.repository
        contract = self.contract_for(run_id, repository)
        if stone.contract_hash != contract.digest():
            raise DevelopmentPaused("development_contract_changed")
        evidence = self.development.fresh_evidence(stone.milestone_id,
            kind=WORKSPACE_EVIDENCE, commit="", env_fingerprint=contract.digest())
        if evidence:
            payload = json.loads(evidence.payload_json)
            workspace = self.workspaces.restore(**payload, requested_repo=repository)
        else:
            # An unbound preexisting clone is not adopted or deleted here.
            workspace = self.workspaces.clone(stone.milestone_id, repository, slug=bound.format + "-adapter")
            self.development.record_evidence(stone.milestone_id,
                kind=WORKSPACE_EVIDENCE, commit="", env_fingerprint=contract.digest(),
                ok=True, summary="Core-bound isolated extension workspace", payload=asdict(workspace))
        binding = _Binding(self, run_id, stone.milestone_id, bound, workspace, contract.digest())
        return _Driver(binding)

    async def drive(self, run_id: str, *, max_rounds=DR.MAX_ROUNDS) -> DevelopmentResult:
        """No timer-based quota retry. Existing Owner boundary resolution resumes.

        A process loss in an open phase is held, never retried automatically.
        Cost claims additionally block unknown earlier effects across phase IDs.
        """
        try:
            lock = DR._open_lock()
        except DR.DriverLocked:
            return DevelopmentResult("busy", reason="development_busy")
        driver = None
        try:
            driver = self.factory(run_id)
            stone = self.development.milestone(driver.binding.milestone_id)
            if stone.state in A.PARKED_STATES:
                if not driver.resume_from_original_task():
                    return driver.parked_result()
            if self.development.open_phases(stone.milestone_id):
                raise DevelopmentPaused("cost_recovery_required", dispatch_started=True,
                                        provider="codex", cost_status="unknown")
            stone = self.development.milestone(stone.milestone_id)
            latest = self.development.phases(stone.milestone_id, limit=1)
            if latest and latest[0]["state"] in {"succeeded", "failed"} and (
                    (stone.state in {A.BUILDING, A.FIXING} and latest[0]["kind"] == "build")
                    or (stone.state == A.REVIEWING and latest[0]["kind"] == "review")):
                # Legacy Driver closes a model phase before its next state is
                # durable. A crash in that gap is not a fresh build/review.
                raise DevelopmentPaused("cost_recovery_required", dispatch_started=True,
                                        provider="codex")
            await driver.run(stone.milestone_id, max_rounds=max_rounds)
            driver.binding.check()
            stone = self.development.milestone(stone.milestone_id)
            if stone.state in A.PARKED_STATES:
                return driver.parked_result()
            if stone.state == A.READY:
                checkpoint = driver.publication(stone.last_commit)
                return DevelopmentResult("ready", commit=stone.last_commit,
                    checkpoint_id=checkpoint, provider="codex")
            return DevelopmentResult("pending", reason=stone.state.lower())
        except asyncio.CancelledError:
            if driver:
                driver.hold(DevelopmentPaused("cost_recovery_required", dispatch_started=True,
                                              provider="codex", cost_status="unknown"))
            raise
        except DevelopmentPaused as exc:
            if driver:
                driver.hold(exc)
            return exc.result
        except (W.WorkspaceError, A.LedgerError, EA.ActivationRefused, OSError, ValueError) as exc:
            pause = DevelopmentPaused(getattr(exc, "reason", "development_unavailable"))
            if driver:
                driver.hold(pause)
            return pause.result
        finally:
            lock.close()


class _Binding:
    def __init__(self, service, run_id, milestone_id, bound, workspace, contract_hash):
        self.service, self.run_id, self.milestone_id = service, run_id, milestone_id
        self.bound, self.workspace, self.contract_hash = bound, workspace, contract_hash

    def check(self):
        run, bound = self.service._bound(self.run_id)
        if (bound != self.bound or run.development_ref != self.milestone_id
                or self.service.development.milestone(self.milestone_id).contract_hash != self.contract_hash):
            raise DevelopmentPaused("development_binding_changed")
        return self.service.workspaces.restore(**asdict(self.workspace), requested_repo=self.workspace.repo)

    @contextmanager
    def scope(self, kind):
        self.check()
        phases = [p for p in self.service.development.open_phases(self.milestone_id) if p["kind"] == kind]
        if len(phases) != 1:
            raise DevelopmentPaused("development_phase_binding")
        with D.task_cost_scope(self.service.ledger, task_id=self.bound.task_id,
                run_id=self.run_id, phase="extension_" + kind,
                operation_id=phases[0]["phase_id"], quote_adapter=self.service.quote_adapter,
                settlement_adapter=self.service.settlement_adapter):
            yield

    def outcome(self, result, *, readonly=False):
        data = result if isinstance(result, dict) else vars(result)
        reason = (data.get("reason") or getattr(data.get("result"), "reason", "") or "")
        if data.get("quota"):
            reason = "quota"
        if data.get("cost_status") == "unknown":
            reason = "cost_recovery_required"
        self.service.ledger.record_event(self.run_id, "provider_route",
            "Task-bound extension provider call", ref=json.dumps({
                "provider": data.get("provider", "codex"),
                "billing_mode": data.get("billing_mode", "unknown"),
                "auth": data.get("auth", ""), "phase": "assessment" if isinstance(result, dict) else "specialist",
                "stage": "observed", "dispatch_started": bool(data.get("dispatch_started", True)),
                "usage_reported": bool(data.get("usage_reported"))}, separators=(",", ":")))
        if reason in BLOCKERS:
            raise DevelopmentPaused(reason, dispatch_started=data.get("dispatch_started", True),
                provider=data.get("provider", "codex"), cost_status=data.get("cost_status", ""),
                billing_mode=data.get("billing_mode", "unknown"), auth=data.get("auth", ""),
                usage_reported=bool(data.get("usage_reported")),
                terminal_readonly_quota=(readonly and reason == "quota" and self._terminal_lead(data)))
        self.check()

    def _terminal_lead(self, data):
        """A reply label alone cannot authorize repeating even a read-only call.

        The physical dispatch must be the current Core review phase, recorded
        finished (never unknown) by the existing launcher/cost seam, and its
        exact reservation must have a settled actual cost. No builder result
        calls this exception. Owner resume still consumes the normal receipt.
        """
        if (data.get("provider") != "codex" or data.get("cost_status") != "settled"
                or data.get("dispatch_started") is not True):
            return False
        phases = [p for p in self.service.development.open_phases(self.milestone_id)
                  if p["kind"] == "review"]
        if len(phases) != 1:
            return False
        self.check()
        with self.service.ledger._open() as connection:
            row = connection.execute(
                "SELECT 1 FROM agent_provider_invocations i "
                "JOIN agent_cost_reservations c ON c.reservation_id=i.reservation_id "
                "WHERE i.invocation_id=? AND i.reservation_id=? AND i.task_id=? "
                "AND i.run_id=? AND i.phase='extension_review' AND i.operation_id=? "
                "AND i.provider='codex' AND i.state='finished' AND i.finished_at IS NOT NULL "
                "AND c.state='settled' AND c.actual_cents IS NOT NULL",
                (data.get("cost_invocation_id", ""), data.get("cost_reservation_id", ""),
                 self.bound.task_id, self.run_id, phases[0]["phase_id"])).fetchone()
        return row is not None


class _Builder:
    name = "codex"

    def __init__(self, binding):
        self.binding = binding

    def capability(self):
        return B.WRITER

    def blocked_reason(self):
        return ""

    async def status(self):
        status = await P.ensure_subscription("codex")
        if not status.available:
            raise DevelopmentPaused(status.reason, provider="codex", billing_mode=status.billing_mode, auth=status.auth)
        return status.as_dict()

    async def build(self, task, workspace):
        started = time.monotonic()
        with self.binding.scope("build"):
            result = await SP.run_specialist(SP.SpecialistRequest("builder/codex",
                task.instruction, workspace, context=task.context, run_id=self.binding.run_id))
        self.binding.outcome(result)
        return B.BuildOutcome(B.OK if result.result.ok else B.FAILED, "codex",
            text=result.result.raw_excerpt, elapsed=time.monotonic() - started,
            pgid=result.pgid, started_at=result.started_at, executable=result.executable,
            detail=result.result.reason)


class _Lead:
    provider = "codex"

    def __init__(self, binding):
        self.binding = binding

    async def judge(self, *, context, allowed_builders, tier=None, **validation):
        # Keep the existing lead schema and validator; provider default model
        # stays fixed even if the old Driver would prefer a larger model tier.
        payload = LEAD.build_request(context=context, allowed_builders=allowed_builders, model="")
        payload["input"] = LEAD.INSTRUCTION + "\n\n" + payload["input"]
        with self.binding.scope("review"):
            response = await SubscriptionTransport("codex")(payload)
        self.binding.outcome(response, readonly=True)
        if not response.get("ok"):
            raise DevelopmentPaused(response.get("reason") or "provider_failed",
                dispatch_started=response.get("dispatch_started", True), provider="codex",
                cost_status=response.get("cost_status", ""))
        verdict = LEAD.validate(response["text"], allowed_builders=set(allowed_builders), **validation)
        if verdict.verdict == LEAD.ESCALATE or verdict.next_action not in {"ready", "fix", "build", "human_required"}:
            # A completed quality review is not an account/usage outage. Keep
            # its actual decision before stopping this bounded development.
            self.binding.service.development.record_decision(self.binding.milestone_id,
                role=A.ROLE_LEAD, verdict=verdict.verdict, next_action=verdict.next_action,
                rationale=SP.redact_specialist_output(verdict.rationale)[:2000],
                digest=hashlib.sha256(json.dumps(verdict.as_dict(), sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest())
            raise DevelopmentPaused("extension_review_required", dispatch_started=True, provider="codex")
        verdict.model, verdict.tokens = "", response.get("tokens")
        return verdict


class _Driver(DR.Driver):
    def __init__(self, binding):
        self.binding = binding
        super().__init__(binding.service.development, workspace=binding.workspace.path,
            adapters={"codex": _Builder(binding)}, lead=_Lead(binding),
            publisher=binding.service.publisher, preflight=self._preflight)

    async def _preflight(self, ledger, milestone_id, adapters, **kwargs):
        self.binding.check()
        await adapters["codex"].status()
        return {"builder_options": {"codex": CAP.CapacityReport(A.ROLE_BUILDER, "codex", CAP.AVAILABLE)}}

    def publication(self, commit):
        evidence = self.ledger.fresh_evidence(self.binding.milestone_id,
            kind="checkpoint_ref", commit=commit, env_fingerprint="")
        payload = json.loads(evidence.payload_json) if evidence else {}
        checkpoint = payload.get("checkpoint_id", "")
        verifier = EA.ExtensionActivation(self.binding.service.ledger,
            development=self.ledger, publisher=self.publisher, file_runtime=self.binding.service.file_runtime)
        verifier._publication(self.binding.run_id, self.binding.milestone_id, commit, checkpoint)
        return checkpoint

    async def _test_phase(self, milestone_id, *, now=0.0):
        self.binding.check()
        phase_id = self.ledger.start_phase(milestone_id, kind="test", now=now)
        commit = self.ledger.milestone(milestone_id).last_commit
        self.publication(commit)
        verifier = EA.ExtensionActivation(self.binding.service.ledger,
            development=self.ledger, publisher=self.publisher, file_runtime=self.binding.service.file_runtime)
        source = verifier._source(commit)
        with tempfile.TemporaryDirectory(prefix="solvio-extension-gate-") as directory:
            Path(directory, EA.ENTRYPOINT).write_bytes(source)
            invocation = EF.invocation(self.binding.bound, os.path.realpath(directory), EA.ENTRYPOINT,
                {EA.ENTRYPOINT: hashlib.sha256(source).hexdigest()}, file_runtime=self.binding.service.file_runtime)
            detail = None
            if self.binding.bound.format == "tables":
                from solvio.agent_runtime import table_report_contract as TC
                detail = await TC.gate_report(invocation)
                ok = detail["ok"]
            else:
                ok = await verifier._gate_for(invocation, self.binding.bound.format)
            reason = "" if ok else "extension_gate_failed"
            diagnostic = ("; case=" + str(detail["case_index"]) + "; stage=" + detail["stage"]
                + "; reason=" + detail["reason"] + "; execution=" + detail["execution_status"]
                if detail and not ok else "")
        self.binding.check()
        self.publication(commit)
        evidence = self.ledger.record_evidence(milestone_id, kind="test_report", commit=commit,
            env_fingerprint=EF.environment_fingerprint(self.binding.bound.format,
                file_runtime=self.binding.service.file_runtime), ok=ok,
            summary="Core offline " + self.binding.bound.format.upper() + " gate: " + ("passed" if ok else reason + diagnostic),
            payload={"core_fixed": True, "configured_cases": self._case_count(), "ok": ok,
                     "contract_digest": self.binding.bound.contract_digest, "artifact_sha256": hashlib.sha256(source).hexdigest(),
                     **({"result_detail": detail} if detail is not None else {})})
        # This is one independent Core measurement, not two claimed test runs.
        self._independent = evidence
        self.ledger.finish_phase(phase_id, state="succeeded" if ok else "failed", summary=reason or "Core gate passed")
        self.ledger.set_fields(milestone_id, current_task="", now=now)
        M.transition(self.ledger, milestone_id, A.REVIEWING, gate_evidence_id=evidence,
                     summary="Core offline gate completed", now=now)
        return evidence

    def _latest_gate(self, milestone_id, commit):
        evidence = self.ledger.fresh_evidence(milestone_id, kind="test_report", commit=commit,
            env_fingerprint=EF.environment_fingerprint(self.binding.bound.format,
                file_runtime=self.binding.service.file_runtime))
        self._independent = evidence.evidence_id if evidence else ""
        return self._independent

    def _case_count(self):
        if self.binding.bound.format == "tables":
            from solvio.agent_runtime import table_report_contract as TC
            return len(TC.gate_cases())
        return len(EA.gate_cases(self.binding.bound.format))

    def hold(self, pause):
        # Closing a terminal quota phase without its hold would allow the next
        # process to mistake it for a new build. One existing-ledger transaction
        # keeps the original open phase if any write or the process fails here.
        phases = self.ledger.open_phases(self.binding.milestone_id)
        self.ledger._db.execute("BEGIN IMMEDIATE")
        try:
            self._hold(pause)
            self.ledger._db.commit()
        except BaseException:
            self.ledger._db.rollback()
            raise
        # Preserve the existing PID/starttime/executable ownership check. The
        # durable hold precedes cleanup, so a crash while reaping cannot turn
        # an interrupted provider phase into a fresh authorized invocation.
        for phase in phases:
            self._reap(phase)

    def _hold(self, pause):
        milestone_id = self.binding.milestone_id
        for phase in self.ledger.open_phases(milestone_id):
            self.ledger.finish_phase(phase["phase_id"], state="interrupted" if pause.result.dispatch_started else "refused",
                                     summary=pause.result.reason)
        self.ledger.record_evidence(milestone_id, kind=PAUSE_EVIDENCE, commit="",
            env_fingerprint=self.binding.contract_hash, ok=False, summary=pause.result.reason,
            payload=asdict(pause.result))
        stone = self.ledger.milestone(milestone_id)
        if stone.state not in A.TERMINAL_STATES | A.PARKED_STATES:
            # Test failures normally remain repairable. A process/binding failure
            # at the test seam is still held without changing the old state graph.
            if stone.state == A.TESTING:
                evidence = self.ledger.record_evidence(milestone_id, kind="test_report",
                    commit=stone.last_commit, env_fingerprint=EF.environment_fingerprint(
                        self.binding.bound.format, file_runtime=self.binding.service.file_runtime),
                    ok=False, summary=pause.result.reason)
                M.transition(self.ledger, milestone_id, A.REVIEWING,
                    gate_evidence_id=evidence, summary=pause.result.reason)
            M.park(self.ledger, milestone_id, category="FAILED_NEEDS_OWNER",
                kind="policy_refusal", question=pause.result.reason)

    def parked_result(self):
        evidence = self.ledger.fresh_evidence(self.binding.milestone_id,
            kind=PAUSE_EVIDENCE, commit="", env_fingerprint=self.binding.contract_hash)
        return (DevelopmentResult(**json.loads(evidence.payload_json)) if evidence else
                DevelopmentResult("paused", reason="development_owner_decision"))

    def resume_from_original_task(self):
        """Consume only the original Core's confirmed same-route Owner resume.

        Its cumulative wait booking identifies this receipt even when the Core
        has not yet cleared status=resuming. Consuming it and answering the
        existing development boundary share one Autopilot transaction. A later
        hold cannot replay that receipt. Nothing extends grant, time or budget.
        """
        self.binding.check()
        run = self.binding.service.ledger.get_run(self.binding.run_id)
        try:
            wait = json.loads(run.boundary or "{}")["provider_wait"]
        except (ValueError, KeyError, TypeError):
            return False
        previous = self.parked_result()
        if (run.state != S.WAITING_CAPABILITY or not previous.resume_allowed
                or wait.get("status") != "resuming" or wait.get("phase") != "development"
                or wait.get("provider") != previous.provider or wait.get("provider") != "codex"
                or wait.get("reason") != previous.reason or not wait.get("resume_allowed")
                or wait.get("requested_billing_mode", wait.get("billing_mode")) != "subscription"
                or run.provider_wait_seconds <= 0):
            return False
        receipt = hashlib.sha256(json.dumps([run.run_id, run.provider_wait_seconds, wait],
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.ledger._db.execute("BEGIN IMMEDIATE")
        try:
            consumed = self.ledger.fresh_evidence(self.binding.milestone_id,
                kind="extension_resume_v1", commit=receipt, env_fingerprint=self.binding.contract_hash)
            stone = self.ledger.milestone(self.binding.milestone_id)
            boundaries = self.ledger.open_boundaries(self.binding.milestone_id)
            if consumed or stone.state != A.HUMAN_REQUIRED or len(boundaries) != 1:
                self.ledger._db.rollback()
                return False
            if boundaries[0]["question"] != previous.reason:
                self.ledger._db.rollback()
                return False
            self.ledger.resolve_boundary(boundaries[0]["boundary_id"], "Original task Owner resume: " + receipt)
            M.transition(self.ledger, self.binding.milestone_id, stone.state_before_park,
                         summary="Original task Owner confirmed continuation")
            self.ledger.record_evidence(self.binding.milestone_id, kind="extension_resume_v1",
                commit=receipt, env_fingerprint=self.binding.contract_hash, ok=True,
                summary="Consumed original task Owner resume", payload={"run_id": run.run_id})
            self.ledger._db.commit()
            return True
        except BaseException:
            self.ledger._db.rollback()
            raise
