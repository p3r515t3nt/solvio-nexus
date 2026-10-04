"""Public HTTPS task -> actual subscribed protocol -> native extension -> same task.

Only the provider/auth device is synthetic. The local CLI is an actual process,
all claims/stores/Driver/publication/gates/activation/router/textutil are real.
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_agent_task_entry as ENTRY
from test_agent_document_task_entry import body
from test_agent_extension_development import ADAPTER
from solvio.agent_runtime import store as S, planner as PL, specialists as SP
from solvio.agent_runtime import cost_dispatch as D, costs as C, document_contract as DC
from solvio.agent_runtime.extension_runtime import attach_document_runtime
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.autopilot.store import AutopilotLedger
from solvio.autopilot import capacity as CAP, evidence as EV, lead as LEAD
from solvio.specialists import providers as P, launcher as L
from solvio.specialists.subscription import SubscriptionTransport
from solvio.proactive.store import ProactiveStore
from solvio.security.mobile_approval import browser_sessions as B

DOCUMENT = b'{\\rtf1\\ansi Actual original input.}'


def cli(root, ledger, *, repair_first=False):
    executable = root / 'document-cli'
    executable.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, sqlite3, sys, time
folder = pathlib.Path(__file__).parent
config = json.loads((folder/'fixture.json').read_text())
prompt = sys.stdin.read()
with sqlite3.connect('file:'+config['ledger']+'?mode=ro', uri=True) as db:
    claims = list(db.execute("SELECT run_id,phase,state FROM agent_provider_invocations WHERE state='claimed'"))
    assert len(claims)==1, claims
    run, phase, state = claims[0]
    grants = json.loads(db.execute('SELECT capabilities FROM agent_task_grants WHERE run_id=?',(run,)).fetchone()[0])
    arguments = next(g['constraints'] for g in grants if g['name']=='document_extract_text')
    selection = db.execute('SELECT active_artifact,pending_artifact FROM agent_extension_selection WHERE run_id=?',(run,)).fetchone()
    milestone = db.execute('SELECT development_ref FROM agent_runs WHERE run_id=?',(run,)).fetchone()[0]
old = [json.loads(line) for line in (folder/'calls.jsonl').read_text().splitlines()] if (folder/'calls.jsonl').exists() else []
build_ordinal = len([call for call in old if call['phase']=='extension_build'])
gate = None
if phase=='extension_review':
    with sqlite3.connect('file:'+config['development']+'?mode=ro', uri=True) as db:
        commit = db.execute('SELECT last_commit FROM milestones WHERE milestone_id=?',(milestone,)).fetchone()[0]
        gate = db.execute("SELECT ok,commit_sha,evidence_id FROM evidence WHERE milestone_id=? AND kind='test_report' AND commit_sha=? ORDER BY measured_at DESC LIMIT 1",(milestone,commit)).fetchone()
        assert gate is not None, 'The actual Core gate must precede review'
        assert gate[2] in prompt, 'The actual gate reference must reach the lead context'
        assert 'Core offline RTF gate: '+('passed' if gate[0] else 'extension_gate_failed') in prompt, 'The lead must receive the measured gate outcome'
with (folder/'calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'phase':phase, 'run':run, 'claims':claims, 'prompt':prompt, 'gate':gate, 'selected':selection})+'\\n')
if phase=='extension_build':
    adapter = "print('incorrect conversion')\\n" if config['repair_first'] and build_ordinal==0 else config['adapter']
    pathlib.Path('adapter.py').write_text(adapter)
    print(json.dumps({'findings':['Native converter integrated.'], 'evidence':['adapter.py:2'], 'recommended_path':'Core gate', 'uncertainties':[]}))
    sys.exit(0)
if phase=='extension_review':
    reply = ({'verdict':'READY','next_action':'ready','rationale':'The Core RTF gate is green.','findings':[], 'close_findings':[], 'proven':[]} if gate[0] else
        {'verdict':'FIX','next_action':'fix','rationale':'The actual Core RTF gate failed.', 'task':'Replace adapter.py with the native textutil execv adapter described in the contract.', 'findings':[], 'close_findings':[], 'proven':[]})
elif phase=='plan':
    if selection and selection[0] and not selection[1]:
        if config.get('pause_replan'):
            (folder/'replan.pid').write_text(str(os.getpid()))
            time.sleep(30)
        step = {'art':'capability','faehigkeit':'document_extract_text','argumente':arguments,'erfuellt':config.get('fulfils','a1')}
    else:
        step = {'art':'capability_need','vertrag':'rtf_text_v1','ressource':'input_document','erfuellt':'a1'}
    reply = {'schritte':[step], 'anforderungen':{'auskunft':[{'id':'a1','text':'Den Text des beigefuegten Dokuments lesen.'}], 'handlungen':[], 'unklar':[], 'belege':{'mindestens':0}}}
    if 'requirements' in config:
        reply['anforderungen'] = config['requirements']
elif phase=='assessment':
    messages = json.loads(prompt.split('\\n',1)[1])
    message = json.loads(next(m['content'] for m in messages if m['role']=='user'))
    snapshot = json.loads(message['ergebnis'])
    if config.get('delivery_assessment'):
        text = config['expected_text']
        evidence = next(f for f in snapshot['befunde'] if text in f)
        delivery = next(f for f in snapshot['befunde'] if f.startswith('Core-Bereitstellungsbeleg:'))
        assert 'zum Download bereit' in delivery
        assert len(snapshot['quellen']) == 1
        reply = {'beantwortet':[{'id':i,'belege':[evidence,snapshot['quellen'][0]]} for i in ['a1','a2','a3']] + [{'id':'h1','belege':[delivery]}], 'offen':[], 'fehlend':[], 'unsicher':[], 'weiterarbeit_noetig':False}
    else:
        evidence = next(f for f in snapshot['befunde'] if 'Actual original input.' in f)
        reply = {'beantwortet':[{'id':'a1','belege':[evidence]}], 'offen':[], 'fehlend':[], 'unsicher':[], 'weiterarbeit_noetig':False}
else:
    raise AssertionError(phase)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':13,'output_tokens':8}}))
''', encoding='utf-8')
    executable.chmod(0o700)
    (root / 'fixture.json').write_text(json.dumps({'ledger': ledger.path,
        'development': str(root / 'development.db'), 'adapter': ADAPTER,
        'repair_first': repair_first}))
    return executable


@asynccontextmanager
async def world(*, repair_first=False):
    async with ENTRY.world() as w:
        root = Path(S.state_dir()).resolve()
        executable = cli(root, w.ledger, repair_first=repair_first)
        evidence = C.CostEvidence('free_local', 'fixture:document-cli-' + hashlib.sha256(executable.read_bytes()).hexdigest())
        def invocation(*, workdir, model='', timeout=30):
            return L.Invocation(str(executable), (str(executable), '-'), cwd=workdir, timeout=10)
        def quote(provider, invoked):
            require_equal(provider, 'codex')
            require_equal(invoked.executable, str(executable))
            return D.CostQuote(0, evidence)
        w.quote = quote
        w.development = AutopilotLedger(str(root / 'development.db'))
        w.proactive = ProactiveStore(str(root / 'proactive.db'))
        w.orch.planner = PL.Planner(subscription_transport=SubscriptionTransport('codex'))
        w.orch.development = w.development
        w.orch.proactive = w.proactive
        w.orch.cost_quote_adapter = quote
        w.orch.require_task_authority = True
        attach_document_runtime(w.orch)
        def calls():
            path = root / 'calls.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        w.calls = calls
        with patch.dict(os.environ, {'SOLVIO_AUTOPILOT_LOCK': str(root / 'driver.lock')}), \
                patch.object(P, 'codex_status', AsyncMock(return_value=P.ProviderStatus('codex', True, auth='chatgpt', billing_mode='subscription'))), \
                patch.object(P, 'codex_invocation', invocation), \
                patch.object(SP, 'codex_builder_invocation', invocation), \
                patch.object(CAP, 'preflight', AsyncMock(side_effect=AssertionError('broker probe'))), \
                patch.object(EV, 'record_gate', side_effect=AssertionError('foreign workspace test')), \
                patch.object(LEAD.TechnicalLead, 'judge', AsyncMock(side_effect=AssertionError('broker lead'))):
            try:
                yield w
            finally:
                await w.orch.stop()
                w.development.close()


def fresh_runtime(w):
    runtime = Orchestrator(ledger=S.AgentRunLedger(w.ledger.path),
        router=CapabilityRouter(mobile=CapabilityApprovals(w.co, owner_principal='local-owner')),
        control_plane=w.cp, planner=PL.Planner(subscription_transport=SubscriptionTransport('codex')),
        proactive=w.proactive, development=w.development, cost_quote_adapter=w.quote,
        require_task_authority=True)
    attach_document_runtime(runtime)
    return runtime


async def advance(runtime, run_id, *, until=None, limit=100):
    for _ in range(limit):
        await runtime.tick()
        run = runtime.ledger.get_run(run_id)
        if run.terminal or (until and until(run)):
            return run
        drivers = list(runtime._drivers.values())
        if drivers:
            await asyncio.wait(drivers, timeout=0.1)
        # Owner cancellation drains an advance before persisting CANCELLED.
        # Let that separately owned operation finish between synthetic ticks.
        await asyncio.sleep(0)
    raise AssertionError('bounded test did not finish: ' + str(runtime.ledger.get_run(run_id)))


async def t_public_document_need_survives_detach_and_restart_then_completes_same_task():
    async with world() as w:
        response = await w.start(body(DOCUMENT))
        accepted = await response.json()
        require_equal(response.status, 201, str(accepted))
        run_id, task_id = accepted['run_id'], accepted['task_id']
        require_equal(w.calls(), [])
        waiting = await advance(w.orch, run_id, until=lambda r:r.state==S.WAITING_CAPABILITY)
        require_equal(waiting.state, S.WAITING_CAPABILITY, waiting.result_summary)
        require_equal([c['phase'] for c in w.calls()], ['plan'])
        need = w.ledger.steps_for_run(run_id)[0]
        require_equal(need.kind, 'capability_need')
        require(need.dispatch_claimed_at is None)
        require_equal(w.development.milestones(), [], 'intent has not yet commissioned a driver')
        grant = w.orch.task_authority.for_run(run_id)
        require_equal((await w.client.post(B.SESSION_PATH+'/logout', json={}, headers=w.headers)).status, 200)
        fresh = fresh_runtime(w)
        w.orch = fresh  # finalizer owns the reconstructed runtime
        completed = await advance(fresh, run_id)
        require_equal(completed.state, S.SUCCEEDED, completed.result_summary)
        require_equal(completed.task_id, task_id)
        require_equal(fresh.task_authority.for_run(run_id), grant)
        require_equal(len(fresh.ledger.recent_runs()), 1)
        phases = [c['phase'] for c in w.calls()]
        require_equal(phases, ['plan','extension_build','extension_review','plan','assessment'])
        for call in w.calls():
            require_equal(call['run'], run_id)
            if call['phase'] in {'extension_build','extension_review'}:
                require('Actual original input.' not in call['prompt'])
        document = [a for a in fresh.ledger.artifacts_for_run(run_id) if a.kind=='document_result']
        require_equal(len(document), 1)
        require_equal(Path(document[0].path).read_text().strip(), 'Actual original input.')
        require_equal(document[0].sha256, hashlib.sha256(Path(document[0].path).read_bytes()).hexdigest())
        steps = [s for s in fresh.ledger.steps_for_run(run_id) if s.kind=='capability']
        require_equal(len(steps), 1)
        require_equal(steps[0].state, 'succeeded')
        require(steps[0].dispatch_claimed_at is not None)
        require_equal(await w.store.list_pending(), [])
        client = await w.new_client()
        await w.login(client)
        before = len(w.calls())
        status = await client.get('/v1/agent/runs/'+run_id)
        require_equal(status.status, 200)
        require('Actual original input.' in json.dumps(await status.json()))
        await fresh.tick()
        require_equal(len(w.calls()), before, 'status/recovery replayed work')


async def t_cancel_owns_post_activation_replan_and_drains_the_actual_cli():
    async with world() as w:
        response = await w.start(body(DOCUMENT))
        accepted = await response.json()
        require_equal(response.status, 201, str(accepted))
        run_id, task_id = accepted['run_id'], accepted['task_id']
        await advance(w.orch, run_id, until=lambda r:r.state==S.WAITING_CAPABILITY)
        root = Path(S.state_dir()).resolve()
        fixture = root / 'fixture.json'
        fixture.write_text(json.dumps({**json.loads(fixture.read_text()), 'pause_replan': True}))
        runtime = fresh_runtime(w)
        w.orch = runtime
        progressing = asyncio.create_task(advance(runtime, run_id))
        pid = None

        def alive():
            if pid is None:
                return False
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        try:
            for _ in range(200):
                marker = root / 'replan.pid'
                if marker.exists():
                    pid = int(marker.read_text())
                    break
                if progressing.done():
                    await progressing
                    raise AssertionError('replan finished without entering the local CLI')
                await asyncio.sleep(0.05)
            require(pid is not None, 'the actual post-activation plan never started')
            require(alive())
            require_equal([c['phase'] for c in w.calls()],
                          ['plan', 'extension_build', 'extension_review', 'plan'])
            claims = D.invocations(runtime.ledger, task_id)
            current = [c for c in claims if c['state']=='claimed']
            require_equal(len(current), 1)
            require_equal((current[0]['run_id'], current[0]['phase']), (run_id, 'plan'))
            require_equal(runtime.ledger.get_run(run_id).state, S.RUNNING)
            require_equal(await asyncio.wait_for(runtime.cancel(run_id), timeout=5), True)
            require(not alive(), 'cancel confirmed while the replanning CLI was still alive')
            await asyncio.wait_for(progressing, timeout=2)
            require_equal(runtime.ledger.get_run(run_id).state, S.CANCELLED)
            claims = D.invocations(runtime.ledger, task_id)
            final = next(c for c in claims if c['invocation_id']==current[0]['invocation_id'])
            require_equal(final['state'], 'unknown', 'termination is not a provider completion receipt')
            require_equal([s for s in runtime.ledger.steps_for_run(run_id) if s.dispatch_claimed_at], [])
            require_equal([a for a in runtime.ledger.artifacts_for_run(run_id) if a.kind=='document_result'], [])
            before = len(w.calls())
            await runtime.tick()
            require_equal(len(w.calls()), before, 'cancelled continuation was replayed')
        finally:
            if not progressing.done():
                progressing.cancel()
            await asyncio.gather(progressing, return_exceptions=True)
            if alive():
                # A failing cancellation regression must not leave a child alive.
                os.killpg(pid, signal.SIGKILL)
                for _ in range(100):
                    if not alive():
                        break
                    await asyncio.sleep(0.01)
            require(not alive(), 'local test process survived cleanup')


async def t_public_failed_core_gate_repairs_adapter_and_completes_original_task():
    async with world(repair_first=True) as w:
        response = await w.start(body(DOCUMENT))
        accepted = await response.json()
        require_equal(response.status, 201, str(accepted))
        run_id, task_id = accepted['run_id'], accepted['task_id']
        grant = w.orch.task_authority.for_run(run_id)
        completed = await advance(w.orch, run_id)
        require_equal(completed.state, S.SUCCEEDED, completed.result_summary)
        pending = list(w.orch._advances.values())
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=3)
        require_equal(completed.task_id, task_id)
        require_equal(w.orch.task_authority.for_run(run_id), grant)
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal(len(w.development.milestones()), 1)
        require_equal([c['phase'] for c in w.calls()], ['plan', 'extension_build',
            'extension_review', 'extension_build', 'extension_review', 'plan', 'assessment'])
        reviews = [call for call in w.calls() if call['phase']=='extension_review']
        require_equal([call['gate'][0] for call in reviews], [0, 1])
        require_equal(reviews[0]['selected'], None, 'Red published checkpoint is not an activated extension')
        bad_commit, good_commit = (call['gate'][1] for call in reviews)
        require(bad_commit != good_commit, 'Correction must produce a different actual Git object')
        for call in w.calls():
            require_equal(call['run'], run_id)
            if call['phase'].startswith('extension_'):
                require('Actual original input.' not in call['prompt'])
        for call, expected in zip(reviews, (False, True)):
            gate = w.development.evidence(call['gate'][2])
            require_equal(gate.kind, 'test_report')
            require_equal(gate.ok, expected)
            measured = json.loads(gate.payload_json)
            require(measured['core_fixed'])
            require_equal(measured['configured_cases'], 2)
            require('executed' not in measured, 'Early gate failure cannot claim every configured case ran')
        milestone = w.development.milestone(completed.development_ref)
        require_equal(milestone.state, 'READY')
        require_equal(milestone.last_commit, good_commit)
        phases = list(reversed(w.development.phases(milestone.milestone_id)))
        require_equal([p['state'] for p in phases if p['kind']=='test'], ['failed', 'succeeded'])
        require_equal([p['state'] for p in phases if p['kind']=='build'], ['succeeded', 'succeeded'])
        publications = w.orch.extension_development.publisher.published(milestone.milestone_id)
        require_equal({commit for _, commit in publications}, {bad_commit, good_commit})
        artifacts = w.ledger.artifacts_for_run(run_id)
        candidates = [a for a in artifacts if a.kind=='extension_candidate']
        require_equal(len(candidates), 1, 'Failed candidate must never reach activation')
        manifest = json.loads(Path(candidates[0].path).read_text())
        require_equal(manifest['commit'], good_commit)
        result = [a for a in artifacts if a.kind=='document_result']
        require_equal(len(result), 1)
        require_equal(Path(result[0].path).read_text().strip(), 'Actual original input.')
        steps = [s for s in w.ledger.steps_for_run(run_id) if s.kind=='capability']
        require_equal(len(steps), 1)
        require_equal(steps[0].state, 'succeeded')
        require(steps[0].dispatch_claimed_at is not None)
        require_equal(await w.store.list_pending(), [], 'Repair creates no additional user approval')
        notices = [notice for notice in await w.proactive.unread() if notice['lauf']==run_id]
        require_equal(len(notices), 1)
        require_equal(notices[0]['zusammenfassung'], completed.result_summary,
                      'Inbox must report the same completed task and retain its run locator')
        require('Actual original input.' in notices[0]['befunde'],
                'The bound completion notice must carry the actual document result')
        require('Quelle: Dokumentquelle SHA-256 '+hashlib.sha256(DOCUMENT).hexdigest() in notices[0]['befunde'],
                'The inbox result must retain the exact original document provenance')
        calls_before = len(w.calls())
        fresh = fresh_runtime(w)
        w.orch = fresh
        await fresh.tick()
        require_equal(len(w.calls()), calls_before, 'Completed correction must not replay after restart')
        status = await w.client.get('/v1/agent/runs/'+run_id)
        require_equal(status.status, 200)
        require('Actual original input.' in json.dumps(await status.json()))
        require_equal(len(w.calls()), calls_before)
        require_equal([n for n in await w.proactive.unread() if n['lauf']==run_id], notices)


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
