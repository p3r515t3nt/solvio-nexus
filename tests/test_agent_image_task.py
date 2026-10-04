"""Public task -> real Core -> installed Hermes transport -> native RPC fixture -> file.

Only provider responses are synthetic. Authorization, planning validation,
dispatch/cost claim, file publication, assessment boundary and HTTPS are real.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as T
import test_native_image_generation as N
from solvio.agent_runtime import planner as PL, specialists as SP, store as S, result_files as F
from solvio.agent_runtime import cost_dispatch as D, costs as C
from solvio.proactive.store import ProactiveStore

OBJECTIVE = 'Erzeuge ein schönes Comic-Hund-Bild und gib mir die Bilddatei.'
REQUIREMENTS = {'auskunft': [], 'handlungen': [{'id':'h1','text':OBJECTIVE}],
                'unklar': [], 'belege': {'mindestens':0}}


class Provider:
    route = {'provider':'codex','billing_mode':'subscription'}
    provider = 'codex'
    timeout = 2.0
    def __init__(self, *, on_assess=None):
        self.calls = []
        self.on_assess = on_assess
    async def __call__(self, payload):
        request = json.loads(payload['input'][-1]['content'])
        if 'ziel' in request:
            self.calls.append('plan')
            require(SP.IMAGE_PROFILE in request['auswahl']['profile'])
            reply = {'schritte':[{'art':'specialist','profil':SP.IMAGE_PROFILE,
                'auftrag':OBJECTIVE,'erfuellt':'h1'}], 'anforderungen':REQUIREMENTS}
        else:
            self.calls.append('assessment')
            snapshot = json.loads(request['ergebnis'])
            evidence = next(f for f in snapshot['befunde'] if f.startswith('Core-Dateibeleg:'))
            if self.on_assess:
                await self.on_assess()
            reply = {'beantwortet':[{'id':'h1','belege':[evidence]}], 'offen':[],
                     'fehlend':[], 'unsicher':[], 'weiterarbeit_noetig':False}
        return {'ok':True,'text':json.dumps(reply),'tokens':0,**self.route}


@asynccontextmanager
async def world(mode='ok', *, on_assess=None):
    async with T.world() as w:
        with N.fixture(mode, timeout=2.0) as (native, config, *_):
            w.native, w.config = native, config
            w.provider = Provider(on_assess=on_assess)
            w.orch.planner = PL.Planner(subscription_transport=w.provider)
            w.orch.cost_quote_adapter = lambda *_: D.CostQuote(0, C.CostEvidence('free_local','test:local-image-rpc'))
            w.orch.proactive = ProactiveStore(str(Path(S.state_dir())/'notices.sqlite3'))
            with patch.object(SP, 'native_research_config', return_value=config), \
                 patch.object(SP, 'native_research_configured', return_value=True):
                result = await w.start(dict(T.BODY, objective=OBJECTIVE))
                require_equal(result.status, 201)
                accepted = await result.json()
                w.run_id, w.task_id = accepted['run_id'], accepted['task_id']
                try:
                    yield w
                finally:
                    await w.orch.stop()


async def advance(w):
    for _ in range(30):
        await w.orch.tick()
        current = w.ledger.get_run(w.run_id)
        if current.terminal or current.state == S.WAITING_USER:
            return current
        await asyncio.sleep(.03)
    raise AssertionError('Core did not reach a bounded result')


async def t_public_image_task_finishes_after_browser_closes_and_fresh_browser_reads_exact_file():
    async with world() as w:
        await w.client.session.close()  # Close the browser, not TestClient's owned Core server.
        run = await advance(w)
        require_equal(run.state, S.SUCCEEDED, run.result_summary)
        require_equal(len(N.method_rows(w.native,'turn/start')),1)
        require_equal(w.provider.calls,['plan','assessment'])
        require_equal(C.CostLedger(w.ledger).view(w.task_id)['counts'], {'settled':1})
        browser = await w.new_client(); await w.login(browser)
        detail = await (await browser.get('/v1/agent/runs/'+w.run_id)).json()
        require_equal(len(detail['dateien']),1)
        descriptor = detail['dateien'][0]
        require_equal(await (await browser.get(descriptor['download_url'])).read(), N.PNG)
        await w.orch.tick()
        require_equal(len(N.method_rows(w.native,'turn/start')),1)
        require_equal(len(F.completion_evidence(S.AgentRunLedger(w.ledger.path),w.run_id)),1)
        notices = await w.orch.proactive.unread()
        require_equal(len([n for n in notices if n['lauf']==w.run_id]),1)


async def t_only_prompt_or_foreign_image_never_becomes_success_and_does_not_retry():
    for mode in ('no_image','foreign_image','invalid_image'):
        async with world(mode) as w:
            run = await advance(w)
            require_equal(run.state,S.FAILED,mode+':'+run.result_summary)
            require_equal(F.describe_files(w.ledger,w.run_id)[0],[])
            require_equal(w.provider.calls,['plan'])
            require_equal(len(N.method_rows(w.native,'turn/start')),1)
            items = await w.orch.proactive.unread()
            require(items[0]['zusammenfassung'].startswith('Auftrag fehlgeschlagen.'))


async def t_image_removed_during_assessment_invalidates_even_a_positive_answer():
    async with world() as w:
        async def remove():
            output = next(a for a in w.ledger.artifacts_for_run(w.run_id) if a.kind=='result_file')
            Path(output.path).unlink()
        w.provider.on_assess = remove
        run = await advance(w)
        require_equal((run.state,run.failure_category),(S.FAILED,'goal_unverified'))
        require_equal(F.describe_files(w.ledger,w.run_id)[0],[])
        require_equal(len(N.method_rows(w.native,'turn/start')),1)


async def t_quota_blocks_at_same_task_without_research_or_paid_fallback():
    async with world('quota_turn') as w:
        run = await advance(w)
        require_equal(run.state,S.WAITING_USER,run.result_summary)
        require_equal(F.describe_files(w.ledger,w.run_id)[0],[])
        for _ in range(3): await w.orch.tick()
        require_equal(len(N.method_rows(w.native,'turn/start')),1)
        require_equal(w.provider.calls,['plan'])


async def t_cancelled_task_cannot_publish_late_native_image_bytes():
    async with world() as w:
        from solvio.agent_runtime.image_generation import NativeImageGenerator
        generate = NativeImageGenerator.generate
        async def late(generator, run, step, planned):
            result = await generate(generator, run, step, planned)
            await w.orch.cancel(run.run_id)
            return result
        with patch.object(NativeImageGenerator,'generate',late):
            run = await advance(w)
        require_equal(run.state,S.CANCELLED)
        require_equal(F.describe_files(w.ledger,w.run_id)[0],[])
        require_equal(len(N.method_rows(w.native,'turn/start')),1)


async def t_adapter_without_native_receipt_cannot_publish_an_image():
    async with world() as w:
        from solvio.agent_runtime.image_generation import NativeImageGenerator
        generate = NativeImageGenerator.generate
        async def missing_proof(generator, run, step, planned):
            require(generator.config.timeout_s <= SP.profile(SP.IMAGE_PROFILE).timeout)
            result = await generate(generator, run, step, planned)
            result.native_proof = None
            return result
        with patch.object(NativeImageGenerator, 'generate', missing_proof):
            run = await advance(w)
        require_equal(run.state, S.FAILED)
        require_equal(F.describe_files(w.ledger, w.run_id)[0], [])
        require_equal(len(N.method_rows(w.native, 'turn/start')), 1)


def t_image_step_requires_explicit_bound_output_and_cannot_use_generic_cli_fallback():
    for bad in ({'auftrag':''},{'erfuellt':''},{'verzichtbar':True}):
        try:
            PL.validate({'schritte':[{'art':'specialist','profil':SP.IMAGE_PROFILE,
                'auftrag':OBJECTIVE,'erfuellt':'h1',**bad}]}, scope='research',
                allowed_profiles={SP.IMAGE_PROFILE},known_capabilities=set(),goal=OBJECTIVE)
        except PL.PlanInvalid: pass
        else: raise AssertionError('Incomplete image step accepted')
    try:
        SP._invocation_for(SP.profile(SP.IMAGE_PROFILE),SP.SpecialistRequest(SP.IMAGE_PROFILE,OBJECTIVE,''))
    except Exception as exc: require_equal(getattr(exc,'reason',''),'image_requires_task_runtime')
    else: raise AssertionError('Image profile borrowed generic coding CLI')


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
