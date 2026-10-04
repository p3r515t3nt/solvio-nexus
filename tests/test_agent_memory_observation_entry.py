"""N4: oeffentlicher HTTPS-Auftrag und nachholbares Beobachtungsangebot.

Echte Eingangsauthentifizierung, TaskSource/Grant und Runtime. Die ersten Tests
pruefen die Zustellnaht; der Durchstich verwendet echte AdaptiveObservations,
Pipeline, SubscriptionTransport, CLI-Starter und Wissenscompiler. Nur die CLI
ist ein lokales Programm mit synthetischer Antwort, kein Anbieteraufruf.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from test_agent_task_entry import world, BODY
from test_agent_personal_context import world as memory_world, Planner
from solvio.agent_runtime import store as S
from solvio.agent_runtime.orchestrator import Orchestrator


class Recipient:
    """Nur die synchrone Zustellnaht, keine fingierte Extraktion."""
    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = []
        self.fail = False

    def offer_task(self, task_id, run_id):
        require_equal(self.ledger.get_run(run_id).task_id, task_id)
        self.calls.append((task_id, run_id))
        if self.fail:
            raise RuntimeError("PRIVATE_QUEUE_FAILURE")
        return True


async def t_https_success_offers_only_the_real_ready_ids_and_replay_keeps_them():
    async with world() as w:
        recipient = Recipient(w.ledger)
        w.orch.memory_observations = recipient
        first = await w.start()
        require_equal(first.status, 201)
        accepted = await first.json()
        ids = (accepted["task_id"], accepted["run_id"])
        require_equal(recipient.calls, [ids])
        require(w.orch.task_starts.ready(ids[1]))
        require_equal(w.orch.task_authority.for_run(ids[1]).receipt_method, "dashboard_session")
        replay = await w.start()
        require_equal(replay.status, 201)
        require_equal(recipient.calls, [ids, ids], "anderer Auftrag aus einem HTTP-Replay")
        require_equal(len(w.ledger.recent_runs()), 1)
        require(not w.orch.offer_task_observation("unrelated-task", ids[1]))
        require_equal(recipient.calls, [ids, ids])


async def t_preparing_admission_is_not_offered_until_recovered_and_ready():
    async with world() as w:
        recipient = Recipient(w.ledger)
        w.orch.memory_observations = recipient
        with patch.object(w.orch.task_starts, "finish", side_effect=RuntimeError("paused-local-fixture")):
            response = await w.start()
            require_equal(response.status, 202)
            accepted = await response.json()
            require_equal(accepted["annahme"], "preparing")
            await w.orch.tick()
            require_equal(recipient.calls, [])
        await w.orch.tick()  # dauerhafte Annahme fertigstellen
        require(w.orch.task_starts.ready(accepted["run_id"]))
        await w.orch.tick()  # jetzt erst darf _advance anbieten und anfangen
        require_equal(recipient.calls, [(accepted["task_id"], accepted["run_id"])])
        require_equal(w.ledger.get_run(accepted["run_id"]).state, S.PLANNING)


async def t_queue_failure_does_not_fail_http_and_new_runtime_reoffers_before_work():
    async with world() as w:
        broken = Recipient(w.ledger)
        broken.fail = True
        w.orch.memory_observations = broken
        response = await w.start()
        require_equal(response.status, 201)
        accepted = await response.json()
        ids = (accepted["task_id"], accepted["run_id"])
        require_equal(broken.calls, [ids])
        require_equal(w.ledger.get_run(ids[1]).state, S.CREATED)
        # Erste Instanz endet nach dauerhafter Annahme, vor erfolgreichem Offer.
        recipient = Recipient(w.ledger)
        after = Orchestrator(ledger=S.AgentRunLedger(w.ledger.path), router=w.router,
                             require_task_authority=True, memory_observations=recipient)
        await after.reconcile()
        await after.tick()
        require_equal(recipient.calls, [ids])
        require_equal(after.ledger.get_run(ids[1]).state, S.PLANNING)
        require_equal(after.ledger.get_task(ids[0]).objective, BODY["objective"])


async def t_unauthenticated_or_rejected_start_never_offers_an_observation():
    async with world() as w:
        recipient = Recipient(w.ledger)
        w.orch.memory_observations = recipient
        response = await w.client.post("/v1/agent/tasks", json={"task": BODY})
        require_equal(response.status, 401)
        response = await w.start(dict(BODY, origin="trusted_interactive_app"))
        require_equal(response.status, 400)
        require_equal(recipient.calls, [])
        require_equal(w.ledger.recent_runs(), [])


async def t_grant_revoked_during_personal_lookup_prevents_model_dispatch():
    async with world() as w, memory_world() as memory:
        planner = Planner()
        w.orch.personal_memory = memory.memory
        w.orch.planner = planner
        w.orch.require_task_authority = True
        accepted = await (await w.start()).json()
        run_id = accepted["run_id"]
        await w.orch.tick()
        require_equal(w.ledger.get_run(run_id).state, S.PLANNING)
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_lookup(*args, **kwargs):
            entered.set()
            await release.wait()
            return []

        with patch.object(memory.memory, "search", held_lookup):
            ticking = asyncio.create_task(w.orch.tick())
            await asyncio.wait_for(entered.wait(), 1)
            grant = w.orch.task_authority.for_run(run_id)
            require(w.orch.task_authority.revoke(grant.reference, "fixture:owner-revocation"))
            release.set()
            await ticking
        require_equal(planner.contexts, [])
        require_equal(w.ledger.get_run(run_id).failure_category, "policy_denied")
        require_equal(w.ledger.get_run(run_id).state, S.FAILED)


def local_memory_cli(folder, ledger_path):
    """Derselbe Codex-JSONL-Vertrag wie test_subscription_planner, echter Prozess."""
    from test_subscription_planner import codex_answer
    proposal = {"proposals": [{"statement": "Bevorzugt kurze Antworten.", "kind": "stated",
        "memory_type": "preference", "subject": "pref:antwortlaenge", "about": "self",
        "sensitivity": "personal", "flags": []}]}
    executable = folder / "codex-memory"
    settings = {"ledger": ledger_path, "answer": codex_answer(json.dumps(proposal),
                {"input_tokens": 40, "output_tokens": 20})}
    (folder / "memory-cli.json").write_text(json.dumps(settings))
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, sqlite3, sys
from pathlib import Path
folder = Path(__file__).parent
if sys.argv[1:3] == ['login', 'status']:
    print('Logged in using ChatGPT')
    raise SystemExit(0)
settings = json.loads((folder / 'memory-cli.json').read_text())
prompt = sys.stdin.read()
if 'Ich bevorzuge kurze Antworten.' not in prompt or '--json' not in sys.argv:
    raise SystemExit('wrong bounded extractor input')
with sqlite3.connect('file:' + settings['ledger'] + '?mode=ro', uri=True) as db:
    claims = db.execute("SELECT task_id,run_id,activity_id FROM agent_provider_invocations "
                        "WHERE state='claimed' AND phase='adaptive_extract'").fetchall()
if len(claims) != 1 or not all(claims[0]):
    raise SystemExit('missing bound physical cost claim before CLI')
with (folder / 'memory-calls.jsonl').open('a') as log:
    log.write(json.dumps({'claim': claims[0], 'argv': sys.argv[1:]}) + '\\n')
print(settings['answer'])
''')
    executable.chmod(0o700)
    return executable


async def t_public_task_learns_through_local_subscription_cli_and_next_task_reads_it():
    from solvio.agent_runtime import costs as C, cost_dispatch as D
    from solvio.contracts.trust import SourceType, TrustLevel
    from solvio.knowledge.service import compile_knowledge
    from solvio.memory.adaptive import candidates as MC
    from solvio.memory.adaptive.extractor import SubscriptionExtractor
    from solvio.memory.adaptive.observations import AdaptiveObservations
    from solvio.memory.adaptive.pipeline import AdaptiveMemory
    from solvio.memory.embedding import HashingEmbeddingProvider
    from solvio.memory.service import MemoryService
    from solvio.security.mobile_approval import browser_sessions as B
    from solvio.specialists import providers as P
    from solvio.specialists.subscription import SubscriptionTransport
    from test_agent_personal_context import data

    async with world() as w:
        folder = Path(w.ledger.path).parent
        service = MemoryService(str(folder / "personal-memory"),
                                provider=HashingEmbeddingProvider()).open()
        candidate_store = MC.CandidateStore(str(folder / "candidates"))
        executable = local_memory_cli(folder, w.ledger.path)
        adaptive = AdaptiveMemory(service, candidate_store,
            extractor=SubscriptionExtractor(transport=SubscriptionTransport(timeout=5)))

        def local_quote(provider, invocation):
            require_equal(provider, "codex")
            require_equal(invocation.executable, str(executable))
            return D.CostQuote(0, C.CostEvidence("free_local", "fixture:local-memory-cli"))

        observations = AdaptiveObservations(adaptive, w.ledger, owner_principal="local-owner",
                                            quote_adapter=local_quote)
        w.orch.memory_observations = observations
        adaptive_closed = False
        try:
            objective = "Vergleiche Hotels Hamburg. Ich bevorzuge kurze Antworten."
            with patch.object(P, "resolve", return_value=str(executable)):
                response = await w.start(dict(BODY, objective=objective))
                require_equal(response.status, 201)
                accepted = await response.json()
                # Die Antwort kehrt zurueck, waehrend die reale Queue weiterarbeitet.
                await asyncio.wait_for(adaptive._queue.join(), 5)
            require_equal(adaptive.extractor.last_status["state"], "completed")
            require_equal(adaptive.last_outcome.adopted, 1, adaptive.last_outcome.as_dict())
            records = await service.semantic.memory.active_records()
            require_equal(len(records), 1)
            learned = records[0]
            require_equal(learned.content, "Bevorzugt kurze Antworten.")
            require_equal(learned.source_type, SourceType.SOLVIO_INFERENCE)
            require_equal(learned.trust_level, TrustLevel.AGENT_GENERATED)
            require(any("task:" + accepted["task_id"] in entry.source for entry in learned.provenance))
            require_equal(len(await candidate_store.list_states(MC.ADOPTED)), 1)
            calls = [json.loads(line) for line in (folder / "memory-calls.jsonl").read_text().splitlines()]
            require_equal(len(calls), 1)
            require_equal(calls[0]["claim"][:2], [accepted["task_id"], accepted["run_id"]])
            require('features.shell_tool=false' in calls[0]["argv"])
            require('features.memories=false' in calls[0]["argv"])
            with w.ledger._open() as db:
                invocation = dict(db.execute("SELECT * FROM agent_provider_invocations").fetchone())
                activity = dict(db.execute("SELECT * FROM agent_cost_activities").fetchone())
                reservation = dict(db.execute("SELECT * FROM agent_cost_reservations").fetchone())
            require_equal(invocation["state"], "finished")
            require_equal(activity["state"], "completed")
            require_equal(reservation["state"], "settled")
            require_equal(reservation["actual_cents"], 0)

            # Replay am echten Eingang/Nachlauf macht weder Extraktion noch Evidenz doppelt.
            require_equal((await w.start(dict(BODY, objective=objective))).status, 201)
            require(not w.orch.offer_task_observation(accepted["task_id"], accepted["run_id"]))
            require_equal(adaptive._queue.qsize(), 0)
            require_equal(len((folder / "memory-calls.jsonl").read_text().splitlines()), 1)

            await adaptive.close()
            adaptive_closed = True
            await service.close()
            service = MemoryService(str(folder / "personal-memory"),
                                    provider=HashingEmbeddingProvider()).open()
            vault = folder / "vault"
            await compile_knowledge(str(vault), memory=service.semantic.memory)
            pages = list(vault.rglob("*.md"))
            require(any(learned.content in p.read_text() for p in pages),
                    "tatsaechlich gelerntes Wissen fehlt im kompilierten Vault")
            fresh_hits = await service.search("kurze Antworten")
            require_equal([hit.memory_id for hit in fresh_hits], [learned.id])

            # Frischer authentifizierter Leser, danach neuer oeffentlicher Auftrag.
            require_equal((await w.client.post(B.SESSION_PATH + "/logout", headers=w.headers)).status, 200)
            reader = await w.new_client()
            headers = await w.login(reader)
            response = await reader.get("/v1/agent/runs", headers=headers)
            require_equal(response.status, 200)
            require(accepted["task_id"] in await response.text())
            require(await w.orch.cancel(accepted["run_id"]))
            w.orch.memory_observations = None  # dieser Nachweis fordert keinen zweiten Extraktoraufruf
            followup = dict(BODY, objective="Formuliere kurze Antworten fuer den Hotelvergleich.",
                            client_request_id="request-personal-followup")
            response = await reader.post("/v1/agent/tasks", json={"task": followup}, headers=headers)
            require_equal(response.status, 201)
            next_task = await response.json()
            planner = Planner()
            fresh = Orchestrator(ledger=S.AgentRunLedger(w.ledger.path), router=w.router,
                                  planner=planner, researcher=object(), personal_memory=service,
                                  memory_owner_principal="local-owner",
                                  require_task_authority=True)
            await fresh.reconcile()
            await fresh.tick()
            await fresh.tick()
            require_equal(fresh.ledger.get_run(next_task["run_id"]).state, S.RUNNING)
            rows = data(planner.contexts[0])["treffer"]
            require_equal([row["id"] for row in rows], [learned.id])
            require_equal(rows[0]["lifecycle"], "learned")
            require("nicht bestaetigt" in rows[0]["unsicherheit"])
            require_equal(len((folder / "memory-calls.jsonl").read_text().splitlines()), 1)
        finally:
            if not adaptive_closed:
                await adaptive.close()
            await service.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
