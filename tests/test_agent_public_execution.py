"""N3: public HTTPS build admission, detached execution and fresh public inquiry.

Real browser authentication, runtime/grants/cost ledger, SubscriptionTransport,
process launcher, Git workspace/harvest and proactive inbox. Only the Codex CLI
is a local program. It modifies actual Python code in a temporary repository;
both its own tests and an independent test of the harvested artifact must pass.
No production state, provider, internal create_task start or real device is used.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_agent_task_entry import World
from test_agent_runtime_subscription_flow import _cli, _git, _repository, ForbiddenBroker
from solvio.agent_runtime import planner as PL, specialists as SP, store as S
from solvio.agent_runtime import costs as C, cost_dispatch as D
from solvio.agent_runtime import workspace as W
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.agent_runtime.workspace import WorkspaceManager, harvest_path
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.capabilities.router import CapabilityRouter
from solvio.proactive.store import ProactiveStore
from solvio.security.mobile_approval import browser_sessions as B
from solvio.specialists import providers as P
from solvio.specialists.subscription import SubscriptionTransport

OBJECTIVE = ("Korrigiere remaining_quota in quota.py: erschoepfte Quota darf nie "
             "negativ werden; belege das mit den vorhandenen Tests.")
CODE = "def remaining_quota(limit, used):\n    return max(0, limit - used)\n"
CODE_TEST = '''import unittest
from quota import remaining_quota

class QuotaTests(unittest.TestCase):
    def test_remaining(self): self.assertEqual(remaining_quota(100, 40), 60)
    def test_exact_limit(self): self.assertEqual(remaining_quota(100, 100), 0)
    def test_over_limit(self): self.assertEqual(remaining_quota(100, 130), 0)
    def test_zero_limit(self): self.assertEqual(remaining_quota(0, 1), 0)

if __name__ == '__main__': unittest.main()
'''


def code_cli(folder, ledger_path, mode):
    """Extend the existing N1 local CLI fixture; production dispatch stays real."""
    executable = _cli(folder, ledger_path)
    fixture = json.loads((folder / "fixture.json").read_text())
    fixture.update(objective=OBJECTIVE, code=CODE, mode=mode)
    fixture["plan"]["schritte"][-1]["auftrag"] = "Korrigiere quota.py und pruefe test_quota.py."
    (folder / "fixture.json").write_text(json.dumps(fixture))
    source = executable.read_text().replace("import json, os, sqlite3, sys",
                                           "import json, os, sqlite3, subprocess, sys")
    before = """        Path('ANALYSE.md').write_text(fixture['report'])
        findings = ['ANALYSE.md dokumentiert Quota und Kontingentbehandlung.']"""
    after = """        if fixture['mode'] != 'empty':
            Path('quota.py').write_text(fixture['code'])
            verified = subprocess.run([sys.executable, '-B', 'test_quota.py'],
                                      capture_output=True, text=True)
            if verified.returncode:
                print(verified.stderr, file=sys.stderr)
                raise SystemExit(verified.returncode)
        findings = ['quota.py verhindert negative Restquota; vier Grenzfalltests bestanden.']"""
    require_equal(source.count(before), 1, "the known N1 builder fixture changed")
    source = source.replace(before, after)
    source = source.replace("'cwd':os.getcwd()", "'cwd':os.getcwd(), 'mode':fixture['mode']")
    marker = "if '--json' in sys.argv:\n    print(json.dumps({'type':'item.completed'"
    require_equal(source.count(marker), 1)
    source = source.replace(marker, """if '--json' in sys.argv and fixture['mode'] == 'quota':
    print(json.dumps({'type':'turn.failed', 'error':{'message':'Usage limit reached'}}))
    raise SystemExit(0)
if '--json' in sys.argv:
    print(json.dumps({'type':'item.completed'""")
    executable.write_text(source)
    return executable


class BuildWorld(World):
    def runtime(self):
        ledger = S.AgentRunLedger(self.ledger.path)
        router = CapabilityRouter(mobile=CapabilityApprovals(self.co, owner_principal="local-owner"))
        planner = PL.Planner(broker=self.broker, transport=self.api,
                            subscription_transport=SubscriptionTransport("codex", timeout=10))

        def quote(provider, invocation):
            require_equal(provider, "codex")
            require_equal(invocation.executable, str(self.executable))
            return D.CostQuote(0, C.CostEvidence("free_local", "test:public-local-codex"))

        orch = Orchestrator(ledger=ledger, router=router, control_plane=self.cp,
            planner=planner, proactive=self.proactive, workspaces=self.workspaces,
            require_task_authority=True, cost_quote_adapter=quote)
        caps = AgentCapabilities(orch)
        router.register(SPECS["agent_task_research"], caps.research)
        router.register(SPECS["agent_task_build"], caps.build)
        self.ledger, self.router, self.orch = ledger, router, orch
        return orch

    def mode(self, mode):
        path = self.folder / "fixture.json"
        data = json.loads(path.read_text())
        data["mode"] = mode
        path.write_text(json.dumps(data))

    def calls(self):
        path = self.folder / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def admit(self):
        response = await self.start({"scope": "build", "objective": OBJECTIVE,
            "target_repo": str(self.repo), "client_request_id": "public-build-001"})
        require_equal(response.status, 201, str(await response.json()))
        accepted = await response.json()
        grant = self.orch.task_authority.for_run(accepted["run_id"])
        require(grant is not None)
        require_equal(grant.receipt_method, "dashboard_session")
        require_equal(grant.authorizer, "local-owner")
        require_equal(await self.store.list_pending(), [])
        require_equal(self.calls(), [], "HTTP acceptance must not itself run a model")
        return accepted, grant

    async def detach(self):
        response = await self.client.post(B.SESSION_PATH + "/logout", json={}, headers=self.headers)
        require_equal(response.status, 200)
        require_equal((await self.client.get("/v1/agent/runs")).status, 401)
        # TestClient.close also closes its shared TestServer. Close the browser
        # transport/session itself so the actual Core HTTPS server stays alive.
        await self.client.session.close()

    async def tick_until(self, run_id, state):
        for _ in range(12):
            await self.orch.tick()
            run = self.ledger.get_run(run_id)
            if run.state == state or run.terminal:
                break
        require_equal(run.state, state, run.result_summary)
        return run

    async def fresh_reader(self, principal="local-owner"):
        client = await self.new_client()
        headers = await self.login(client, principal)
        return client, headers

    def verify_artifact(self, branch):
        require(branch, "no claimed artifact without a real harvested ref")
        require_equal(_git(Path(harvest_path()), "show", branch + ":quota.py"), CODE.strip())
        with tempfile.TemporaryDirectory(dir=self.folder) as verified:
            for name in ("quota.py", "test_quota.py"):
                Path(verified, name).write_text(_git(Path(harvest_path()), "show", branch + ":" + name))
            result = subprocess.run([sys.executable, "-B", "test_quota.py"], cwd=verified,
                                    capture_output=True, text=True, timeout=10)
            require_equal(result.returncode, 0, result.stderr)
            require("Ran 4 tests" in result.stderr)
        require_equal(_git(self.repo, "rev-parse", "HEAD"), self.original_head)
        require_equal(_git(self.repo, "status", "--porcelain"), "")
        require_equal((self.repo / "quota.py").read_text(), self.original_code)


@asynccontextmanager
async def world(mode="success"):
    with tempfile.TemporaryDirectory(prefix="solvio-public-build-") as temp:
        folder = Path(temp)
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": temp, "HOME": temp,
                                     "SOLVIO_AGENT_RUNS_DB": str(folder / "agent.sqlite3")}):
            w = await BuildWorld().open(temp)
            try:
                w.folder = folder
                w.repo = _repository(folder)
                w.original_code = "def remaining_quota(limit, used):\n    return limit - used\n"
                (w.repo / "quota.py").write_text(w.original_code)
                (w.repo / "test_quota.py").write_text(CODE_TEST)
                _git(w.repo, "add", "quota.py", "test_quota.py")
                _git(w.repo, "-c", "user.name=Test", "-c", "user.email=test@example.test",
                     "commit", "-q", "-m", "Quota example with failing boundary tests")
                w.original_head = _git(w.repo, "rev-parse", "HEAD")
                baseline = subprocess.run([sys.executable, "-B", "test_quota.py"], cwd=w.repo,
                                          capture_output=True, text=True, timeout=10)
                require_equal(baseline.returncode, 1, "the original code must expose the requested defect")
                require("FAIL: test_over_limit" in baseline.stderr)
                w.executable = code_cli(folder, w.ledger.path, mode)
                w.broker, w.api = ForbiddenBroker(), AsyncMock(side_effect=AssertionError("paid API"))
                w.proactive = ProactiveStore(str(folder / "proactive.sqlite3"))
                w.workspaces = WorkspaceManager(allowed=(str(w.repo.resolve()),))
                with patch.object(P, "resolve", return_value=str(w.executable)), \
                        patch.object(SP, "resolve", return_value=str(w.executable)):
                    w.runtime()
                    w.app["agent_runtime_provider"] = lambda: w.orch
                    yield w
                require_equal(w.broker.calls, [])
                require_equal(w.api.await_count, 0)
            finally:
                await w.close()


async def t_public_build_finishes_after_session_close_and_fresh_reader_never_reexecutes():
    async with world() as w:
        accepted, grant = await w.admit()
        await w.detach()
        final = await w.tick_until(accepted["run_id"], S.SUCCEEDED)
        w.verify_artifact(final.branch_ref)
        require_equal(final.planner_calls, 1)
        require_equal(final.specialist_count, 2)
        require_equal(final.assessment_calls, 0)
        before = w.calls()
        require_equal([c["kind"] for c in before], ["plan", "investigator", "builder"])
        require(all(c["routes_before_dispatch"] >= 1 for c in before))
        require(all(c["cost_claims"] == 1 for c in before))
        require(all(c["running_steps"] == 1 for c in before[1:]))
        require_equal(w.orch.costs.view(final.task_id)["counts"], {"settled": 3})
        notices = [n for n in await w.proactive.unread() if n["lauf"] == final.run_id]
        require_equal(len(notices), 1)
        require(final.branch_ref in notices[0]["zusammenfassung"])

        w.runtime()
        await w.orch.reconcile()
        await w.orch.tick()
        reader, _ = await w.fresh_reader()
        listed = await (await reader.get("/v1/agent/runs")).json()
        require_equal(len(listed["laeufe"]), 1)
        # Resolve from the actually returned public list, not a remembered ID.
        response = await reader.get("/v1/agent/runs/" + listed["laeufe"][0]["id"])
        require_equal(response.status, 200)
        view = await response.json()
        require_equal(view["id"], accepted["run_id"])
        require_equal(view["zustand_code"], S.SUCCEEDED)
        require_equal(view["auftrag"], OBJECTIVE)
        require_equal(view["arbeitsergebnis"], final.branch_ref)
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)
        require_equal(w.calls(), before)
        require_equal(await w.store.list_pending(), [])
        require_equal([n for n in await w.proactive.unread() if n["lauf"] == final.run_id], notices)


async def t_successful_provider_exit_without_code_change_is_not_a_successful_build():
    async with world("empty") as w:
        accepted, _ = await w.admit()
        await w.detach()
        final = await w.tick_until(accepted["run_id"], S.FAILED)
        require_equal(final.failure_category, "no_result")
        require_equal(final.branch_ref, "")
        require_equal([c["kind"] for c in w.calls()], ["plan", "investigator", "builder"])
        reader, _ = await w.fresh_reader()
        view = await (await reader.get("/v1/agent/runs/" + final.run_id)).json()
        require_equal(view["zustand_code"], S.FAILED)
        require_equal(view["arbeitsergebnis"], "")
        require_equal(w.workspaces.harvest_refs(), [])


async def t_public_quota_wait_survives_restart_and_bound_resume_finishes_once():
    async with world("quota") as w:
        accepted, grant = await w.admit()
        await w.detach()
        waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
        require_equal([c["kind"] for c in w.calls()], ["plan"])
        w.runtime()
        await w.orch.reconcile()
        for _ in range(3):
            await w.orch.tick()
        require_equal(len(w.calls()), 1, "quota must not poll or start another provider")
        reader, headers = await w.fresh_reader()
        path = "/v1/agent/runs/" + accepted["run_id"]
        view = await (await reader.get(path)).json()
        require_equal(view["zustand_code"], S.WAITING_USER)
        require("Kontingent" in view["wartet_auf"]["grund"])
        require_equal(json.loads(waiting.boundary)["provider_wait"]["reason"], "quota")
        foreign, other_headers = await w.fresh_reader("other-owner")
        for operation in ("resume", "cancel"):
            require_equal((await foreign.post(path + "/" + operation, json={}, headers=other_headers)).status, 404)
        require_equal(w.ledger.get_run(waiting.run_id).state, S.WAITING_USER)
        w.mode("success")
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 200)
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 409)
        final = await w.tick_until(waiting.run_id, S.SUCCEEDED)
        w.verify_artifact(final.branch_ref)
        view = await (await reader.get(path)).json()
        require_equal(view["zustand_code"], S.SUCCEEDED)
        require_equal(view["auftrag"], OBJECTIVE)
        require_equal(view["arbeitsergebnis"], final.branch_ref)
        require_equal([c["kind"] for c in w.calls()], ["plan", "plan", "investigator", "builder"])
        require_equal(final.planner_calls, 2)
        require_equal(w.orch.costs.view(final.task_id)["counts"], {"settled": 4})
        require_equal(w.orch.task_authority.for_run(final.run_id).reference, grant.reference)
        require_equal(len(w.ledger.runs_for_task(final.task_id)), 1)
        require_equal(await w.store.list_pending(), [])


async def t_public_cancel_of_quota_wait_never_resumes_or_creates_an_artifact():
    async with world("quota") as w:
        accepted, grant = await w.admit()
        await w.detach()
        waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
        reader, headers = await w.fresh_reader()
        path = "/v1/agent/runs/" + waiting.run_id
        require_equal((await reader.post(path + "/cancel", json={}, headers=headers)).status, 200)
        before = w.calls()
        w.mode("success")
        w.runtime()
        await w.orch.reconcile()
        require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 409)
        for _ in range(3):
            await w.orch.tick()
        view = await (await reader.get(path)).json()
        require_equal(view["zustand_code"], S.CANCELLED)
        require_equal(view["arbeitsergebnis"], "")
        require_equal(w.calls(), before)
        require_equal(len(w.ledger.runs_for_task(waiting.task_id)), 1)
        require(not w.orch.task_authority.active(grant.reference,
            task_id=waiting.task_id, run_id=waiting.run_id).allowed)


async def t_invalid_persisted_workspace_is_refused_before_any_resumed_provider_or_clone():
    for corruption in ("workspace_path", "workspace_base", "workspace_branch"):
        async with world("quota") as w:
            accepted, _ = await w.admit()
            await w.detach()
            waiting = await w.tick_until(accepted["run_id"], S.WAITING_USER)
            calls = w.calls()
            outside = w.folder / "outside-the-workspace"
            outside.mkdir()
            (outside / "untouched.txt").write_text("must stay outside the resumed build")
            wrong = {"workspace_path": str(outside), "workspace_base": "",
                     "workspace_branch": "main"}[corruption]
            # Corrupt only this temporary persisted record. The public Owner
            # resume must not turn an incomplete/changed descriptor into a path grant.
            with w.ledger._open() as db:
                db.execute(f"UPDATE agent_runs SET {corruption}=? WHERE run_id=?",
                           (wrong, waiting.run_id))
            real_git = W._git

            def checked_git(*args, **kwargs):
                locations = list(args) + [kwargs.get("cwd", "")]
                require(not any(str(value) == str(outside)
                    or str(value).startswith(str(outside) + os.sep) for value in locations),
                    "the forged workspace reached a Git operation")
                return real_git(*args, **kwargs)

            with patch.object(W, "_git", side_effect=checked_git), \
                    patch.object(w.workspaces, "clone", wraps=w.workspaces.clone) as clone:
                w.runtime()
                await w.orch.reconcile()
                reader, headers = await w.fresh_reader()
                path = "/v1/agent/runs/" + waiting.run_id
                require_equal((await reader.post(path + "/resume", json={}, headers=headers)).status, 200)
                w.mode("success")
                final = await w.tick_until(waiting.run_id, S.FAILED)
                require_equal(final.failure_category, "workspace_conflict", corruption)
                require_equal(clone.call_count, 0, "a broken descriptor must never cause a second clone")
                require_equal(w.calls(), calls, "workspace refusal came after provider dispatch")
                view = await (await reader.get(path)).json()
                require_equal(view["zustand_code"], S.FAILED)
                require_equal(view["arbeitsergebnis"], "")
            require_equal((outside / "untouched.txt").read_text(),
                          "must stay outside the resumed build")
            require_equal(w.workspaces.harvest_refs(), [])


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
