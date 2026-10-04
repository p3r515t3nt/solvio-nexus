"""Echter Bau-Lauf mit Abo-Transport; gestellt ist nur das Anbieter-CLI.

Planer, Anmelde-Tor, Prozessstarter, Runtime-Spezialisten, Workspace-Ernte,
Ledger, Wiederaufbau und Posteingang sind echte Komponenten. Ein Build wird
wie produktiv deterministisch anhand seines Arbeitsergebnisses geprueft;
er beansprucht keinen semantischen Bewertungsaufruf.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

_SANDBOX = tempfile.mkdtemp(prefix="solvio-subscription-flow-")
atexit.register(shutil.rmtree, _SANDBOX, True)
os.environ["SOLVIO_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_SANDBOX, "runs.sqlite3")

from solvio.agent_runtime import inquiry as Q, planner as PL, specialists as SP, store as S
from solvio.agent_runtime import costs as C, cost_dispatch as D
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.agent_runtime.workspace import WorkspaceManager, harvest_path
from solvio.capabilities.router import CapabilityRouter
from solvio.proactive.store import ProactiveStore
from solvio.specialists import providers as P
from solvio.specialists.subscription import SubscriptionTransport


OBJECTIVE = ("Analysiere das Beispielprojekt und schreibe einen nachvollziehbaren "
             "Bericht ANALYSE.md ueber die Behandlung des Kontingents.")
REPORT = ("# Analyse\n\nDas Beispielprojekt beschreibt eine begrenzte Verarbeitung. "
          "Quota und Kontingent bezeichnen die benoetigte Abbruchbedingung.\n")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args], check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo),
             "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"},
        capture_output=True, text=True, timeout=20)
    return result.stdout.strip()


def _repository(folder: Path) -> Path:
    repo = folder / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# Beispielprojekt\nBegrenzte Verarbeitung.\n",
                                    encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "commit", "-q", "-m", "Ausgangslage")
    return repo


def _cli(folder: Path, ledger_path: str) -> Path:
    """Anbietergrenze als echtes Programm mit persistentem Aufrufbeleg.

    Der Status liest eine rein synthetische Anmeldeform. Die normale Antwort
    reist als echte Codex-JSONL-Ereignisse bzw. als Spezialisten-JSON ueber
    stdin/stdout. Weder Planer noch Runtimeadapter werden ersetzt.
    """
    executable = folder / "codex"
    plan = {"schritte": [
        {"art": "specialist", "profil": "investigator/codex",
         "auftrag": "Lies README.md und untersuche die Kontingentbehandlung."},
        {"art": "specialist", "profil": "builder/codex",
         "auftrag": "Schreibe den nachpruefbaren Bericht in ANALYSE.md."}],
        "hinweis": "Analyse mit einem abgelegten Arbeitsergebnis."}
    (folder / "fixture.json").write_text(json.dumps({
        "objective": OBJECTIVE, "plan": plan, "report": REPORT,
        "ledger": ledger_path}), encoding="utf-8")
    (folder / "auth.txt").write_text("Logged in using ChatGPT", encoding="utf-8")
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, sqlite3, sys
from pathlib import Path
folder = Path(__file__).parent
if sys.argv[1:3] == ['login', 'status']:
    print((folder/'auth.txt').read_text(), file=sys.stderr)
    raise SystemExit(0)
assert 'OPENAI_API_KEY' not in os.environ
assert 'ANTHROPIC_API_KEY' not in os.environ
fixture = json.loads((folder/'fixture.json').read_text())
prompt = sys.stdin.read()
if '--json' in sys.argv:
    messages = json.loads(prompt.split('\\n', 1)[1])
    request = json.loads(messages[-1]['content'])
    assert request['ziel'] == fixture['objective']
    assert request['auswahl']['scope'] == 'build'
    assert 'builder/codex' in request['auswahl']['profile']
    kind = 'plan'
    reply = fixture['plan']
else:
    sandbox = sys.argv[sys.argv.index('--sandbox') + 1]
    assert Path('README.md').read_text().startswith('# Beispielprojekt')
    assert fixture['objective'] not in prompt or 'ZIEL DES NUTZERS' in prompt
    kind = 'builder' if sandbox == 'workspace-write' else 'investigator'
    if kind == 'builder':
        Path('ANALYSE.md').write_text(fixture['report'])
        findings = ['ANALYSE.md dokumentiert Quota und Kontingentbehandlung.']
    else:
        findings = ['README.md beschreibt die begrenzte Verarbeitung; Quota braucht eine Abbruchbedingung.']
    reply = {'findings': findings, 'evidence':['README.md:2'],
             'recommended_path': findings[0], 'uncertainties':[]}
with sqlite3.connect('file:'+fixture['ledger']+'?mode=ro', uri=True) as db:
    route_count = db.execute("SELECT count(*) FROM agent_events WHERE kind='provider_route'").fetchone()[0]
    running_steps = db.execute("SELECT count(*) FROM agent_steps WHERE state='running'").fetchone()[0]
    cost_claims = db.execute("SELECT count(*) FROM agent_provider_invocations WHERE state='claimed'").fetchone()[0]
with (folder/'calls.jsonl').open('a') as log:
    log.write(json.dumps({'kind':kind, 'routes_before_dispatch':route_count,
                          'running_steps':running_steps, 'cost_claims':cost_claims,
                          'cwd':os.getcwd()})+'\\n')
if '--json' in sys.argv:
    print(json.dumps({'type':'item.completed', 'item':{
        'type':'agent_message', 'text':json.dumps(reply)}}))
    print(json.dumps({'type':'turn.completed', 'usage':{
        'input_tokens':120, 'output_tokens':40}}))
else:
    print(json.dumps(reply))
''', encoding="utf-8")
    executable.chmod(0o700)
    return executable


class ForbiddenBroker:
    """Jeder Brokerkontakt ist ein Testfehler und wird zusaetzlich gezaehlt."""

    def __init__(self):
        self.calls = []

    def register_principal(self, *args, **kwargs):
        self.calls.append("register_principal")
        raise AssertionError("Broker-Inferenz im Abo-Lauf")

    def open_lease(self, *args, **kwargs):
        self.calls.append("open_lease")
        raise AssertionError("Broker-Lease im Abo-Lauf")


def t_real_subscription_build_harvests_once_and_survives_a_fresh_reader():
    async def scenario(folder: Path):
        state = folder / "state"
        state.mkdir()
        repo = _repository(folder)
        original_head = _git(repo, "rev-parse", "HEAD")
        ledger_path = str(state / "runs.sqlite3")
        executable = _cli(folder, ledger_path)
        broker = ForbiddenBroker()
        broker_transport = AsyncMock(side_effect=AssertionError("API-Transport"))
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": str(state),
                                     "SOLVIO_AGENT_RUNS_DB": ledger_path,
                                     "HOME": str(folder)}), \
                patch.object(P, "resolve", return_value=str(executable)), \
                patch.object(SP, "resolve", return_value=str(executable)):
            ledger = S.AgentRunLedger(ledger_path)
            proactive = ProactiveStore(str(state / "proactive.sqlite3"))
            workspaces = WorkspaceManager(allowed=(str(repo.resolve()),))

            def make_planner():
                return PL.Planner(broker=broker, transport=broker_transport,
                                  subscription_transport=SubscriptionTransport("codex", timeout=10))

            def local_cost_quote(provider, invocation):
                # Kostenfreiheit stammt vom tatsaechlich lokal gestellten
                # Programm, niemals aus dessen synthetischer Abo-Anmeldung.
                require_equal(provider, "codex")
                require_equal(invocation.executable, str(executable))
                return D.CostQuote(0, C.CostEvidence("free_local", "test:local-codex-fixture"))

            orch = Orchestrator(ledger=ledger, planner=make_planner(),
                                router=CapabilityRouter(policy_mode="enforce"),
                                workspaces=workspaces, proactive=proactive,
                                cost_quote_adapter=local_cost_quote)
            task, run = orch.create_task(objective=OBJECTIVE, scope="build",
                                        origin="trusted_interactive_app", principal="test-owner",
                                        target_repo=str(repo))
            for _ in range(12):
                await orch.tick()
                if ledger.get_run(run.run_id).terminal:
                    break
            final = ledger.get_run(run.run_id)
            require_equal(final.state, S.SUCCEEDED, final.result_summary)
            require_equal(ledger.get_task(task.task_id).state, S.TASK_COMPLETED)
            require_equal(final.planner_calls, 1)
            require_equal(final.specialist_count, 2)
            require_equal(final.assessment_calls, 0,
                          "ein Build benutzt deterministische Erntepruefung")
            require(final.branch_ref)
            require_equal(_git(Path(harvest_path()), "show",
                               final.branch_ref + ":ANALYSE.md"), REPORT.strip())
            require_equal(_git(repo, "rev-parse", "HEAD"), original_head)
            require_equal(_git(repo, "status", "--porcelain"), "")
            require(not (repo / "ANALYSE.md").exists())

            calls_path = folder / "calls.jsonl"
            calls_before = calls_path.read_text()
            calls = [json.loads(line) for line in calls_before.splitlines()]
            require_equal([call["kind"] for call in calls],
                          ["plan", "investigator", "builder"])
            require(calls[0]["routes_before_dispatch"] >= 1,
                    "die Route war vor dem Planeraufruf nicht dauerhaft gebunden")
            require(all(call["running_steps"] == 1 for call in calls[1:]),
                    "der Spezialist wurde vor seiner Schrittbuchung gestartet")
            require(all(call["cost_claims"] == 1 for call in calls),
                    "ein physischer Aufruf startete vor seinem Kostenanspruch")
            costs = C.CostLedger(ledger)
            physical_calls = D.invocations(ledger, task.task_id)
            require_equal([row["phase"] for row in physical_calls], ["plan", "specialist", "specialist"])
            require_equal(len({row["invocation_id"] for row in physical_calls}), 3)
            require(all(row["state"] == "finished" for row in physical_calls))
            require_equal(costs.view(task.task_id)["counts"], {"settled": 3})
            require_equal(costs.view(task.task_id)["ai_tool"]["spent_cents"], 0)
            events = ledger.events_for_run(run.run_id)
            routes = [json.loads(event.ref) for event in events if event.kind == "provider_route"]
            require(routes)
            require(all(route["provider"] == "codex" and route["billing_mode"] == "subscription"
                        for route in routes))
            require(any(route["phase"] == "plan" and route["usage_reported"] for route in routes))
            require_equal(len([route for route in routes if route["phase"] == "specialist"]), 2)
            notices = await proactive.unread()
            require_equal(len([n for n in notices if n["lauf"] == run.run_id]), 1)

            # Frische Runtime und neuer SQLite-Leser: kein alter Pythonzustand
            # wird uebergeben, der fertige Auftrag startet trotzdem nicht neu.
            fresh = S.AgentRunLedger(ledger_path)
            restarted = Orchestrator(ledger=fresh, planner=make_planner(),
                                     router=CapabilityRouter(policy_mode="enforce"),
                                     workspaces=workspaces, proactive=proactive,
                                     cost_quote_adapter=local_cost_quote)
            await restarted.reconcile()
            await restarted.tick()
            route = fresh.provider_route_for_run(run.run_id)
            require_equal(route["provider"], "codex")
            require_equal(route["billing_mode"], "subscription")
            require_equal(fresh.get_run(run.run_id).state, S.SUCCEEDED)
            view = Q.run_view(fresh, fresh.get_run(run.run_id))
            require_equal(view["zustand_code"], S.SUCCEEDED)
            require_equal(view["auftrag"], OBJECTIVE)
            require_equal(view["anbieter"], "codex")
            require_equal(view["abrechnung"], "subscription")
            require(view["anbieternutzung_gemeldet"])
            require_equal(calls_path.read_text(), calls_before,
                          "der frische Leser fuehrte den fertigen Auftrag erneut aus")
            require_equal(D.invocations(fresh, task.task_id), physical_calls)
            require_equal(broker.calls, [])
            require_equal(broker_transport.await_count, 0)

    with tempfile.TemporaryDirectory(dir=_SANDBOX) as folder:
        asyncio.run(scenario(Path(folder)))


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
