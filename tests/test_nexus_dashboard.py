"""N5 private browser paths: real HTTPS, owner sessions and temporary Core stores."""
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from test_agent_task_entry import World, BODY
from test_agent_personal_context import record
from solvio.agent_runtime import endpoint, store as S
from solvio.dashboard import endpoint as D
from solvio import memory_endpoint
from solvio.memory.embedding import HashingEmbeddingProvider
from solvio.memory.service import MemoryService
from solvio.control_center.snapshot import ControlCenter
from solvio.control_center.health import HealthBoard, Probe, State
from solvio.control_center import routes
from solvio.proactive.store import ProactiveStore
from solvio.realtime.audio_observations import AudioObservations
from solvio.realtime.satellite_health import SatelliteHealthRegistry
from solvio.memory.adaptive.pipeline import AdaptiveMemory
from solvio.memory.adaptive.candidates import CandidateStore


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix='solvio-dashboard-') as folder:
        memory=MemoryService(str(Path(folder)/'memory'),provider=HashingEmbeddingProvider()).open()
        adaptive=AdaptiveMemory(memory,CandidateStore(str(Path(folder)/'adaptive')))
        notices=ProactiveStore(str(Path(folder)/'inbox.sqlite3'))
        async def forbidden_probe():
            raise AssertionError('viewing the dashboard invoked a provider probe')
        board=HealthBoard([Probe('synthetic','Lokaler Prüfbeleg',forbidden_probe,network=True)])
        center=ControlCenter(SimpleNamespace(proactive_store=notices),board)
        voice=SimpleNamespace(audio_observations=AudioObservations(),satellite_health=SatelliteHealthRegistry())
        original=endpoint.attach
        def attach(app,*args,**kwargs):
            result=original(app,*args,**kwargs)
            memory_endpoint.attach(app,service=memory,adaptive=adaptive)
            routes.attach(app,center)
            D.attach(app,owner_principal='local-owner',environment='isolated_test',voice_server_provider=lambda:voice,router_provider=lambda:w.router)
            return result
        w=World()
        try:
            with patch.object(endpoint,'attach',attach):
                await w.open(folder)
            from solvio.capabilities.memory import MemoryCapabilities, register
            register(w.router, MemoryCapabilities(memory,adaptive))
            w.memory=memory;w.center=center;w.folder=folder;w.voice=voice;w.adaptive=adaptive
            yield w
        finally:
            await w.close()
            await adaptive.close()
            await memory.close()


async def t_page_and_fixed_assets_contain_no_user_data_or_external_runtime():
    async with world() as w:
        guest=await w.new_client()
        page=await guest.get('/dashboard/')
        require_equal(page.status,200)
        text=await page.text()
        require('SOLVIO' in text)
        require(BODY['objective'] not in text)
        require_equal(page.headers['Cache-Control'],'no-store')
        require("frame-ancestors 'none'" in page.headers['Content-Security-Policy'])
        require_equal(page.headers['Permissions-Policy'],'microphone=(self), camera=(), geolocation=()')
        for name in D.FILES:
            r=await guest.get('/dashboard/assets/'+name)
            require_equal(r.status,200,name)
            require_equal(r.headers['Permissions-Policy'],'microphone=(), camera=(), geolocation=()')
        require_equal((await guest.get('/dashboard/assets/../../config.py')).status,404)
        require_equal((await guest.get('/dashboard/assets/not-an-asset')).status,404)
        require_equal((await guest.get('/v1/dashboard/state')).status,401)
        require_equal(w.ledger.recent_runs(),[])


async def t_chat_assets_are_same_origin_modules_under_the_unchanged_csp():
    import re
    require_equal(D.CSP, "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                  "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
                  "frame-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; object-src 'none'")
    for name in ('chat.js', 'canonical.js'):
        require_equal(D.FILES.get(name), 'text/javascript', name)
    # Every module the page loads is one of the served, same-origin assets.
    for name in ('app.js', 'chat.js', 'canonical.js'):
        for target in re.findall(r"from '\./([^']+)'", (D.ASSETS / name).read_text()):
            require(target in D.FILES, f'{name} imports unserved module {target}')
    page = (D.ASSETS / 'index.html').read_text()
    for url in re.findall(r'(?:src|href)="([^"]+)"', page):
        require(url.startswith('/dashboard/') or url.startswith('#'), url)
    async with world() as w:
        guest = await w.new_client()
        for name in ('chat.js', 'canonical.js'):
            r = await guest.get('/dashboard/assets/' + name)
            require_equal(r.status, 200, name)
            require_equal(r.headers['Content-Type'].split(';')[0], 'text/javascript')
            require_equal(r.headers['Content-Security-Policy'], D.CSP)
            require_equal(r.headers['Cache-Control'], 'no-store')
        require_equal(w.ledger.recent_runs(), [])


async def t_served_text_assets_carry_no_control_byte_and_decode_as_utf8():
    """Review-Befund: chat.js trug ein rohes NUL-Byte (Offset 20098) — `file` las
    das ausgelieferte Modul als `data`, grep/Diff/Review-Werkzeuge stolperten.
    Ein Steuerbyte gehoert als Escape-Sequenz in die Quelle, nie als Byte."""
    allowed_control = {0x09, 0x0A, 0x0D}
    text_assets = ['index.html'] + [n for n, kind in D.FILES.items() if kind.startswith('text/')]
    async with world() as w:
        guest = await w.new_client()
        page = await guest.get('/dashboard/')
        require_equal(page.status, 200)
        bodies = {'index.html': await page.read()}
        for name in text_assets:
            if name == 'index.html':
                continue
            r = await guest.get('/dashboard/assets/' + name)
            require_equal(r.status, 200, name)
            bodies[name] = await r.read()
        for name, body in bodies.items():
            stray = sorted({b for b in body if b < 0x20 and b not in allowed_control})
            require_equal(stray, [], f'{name} is served with control bytes {stray}')
            require_equal(body, (D.ASSETS / name).read_bytes(), f'{name} served differs from the file')
            body.decode('utf-8')  # raises on a byte that is not text
        require_equal(w.ledger.recent_runs(), [])


async def t_owner_reads_existing_memory_and_inbox_but_other_sessions_cannot():
    async with world() as w:
        memory_id=await w.memory.semantic.remember(record('Ich bevorzuge ruhige Arbeitsplätze.'))
        for path in ['/v1/dashboard/state','/v1/memory/memories','/v1/memory/memories/'+memory_id+'/provenance','/v1/control/inbox']:
            r=await w.client.get(path)
            require_equal(r.status,200,path)
            require_equal(r.headers['Cache-Control'],'no-store')
        foreign=await w.new_client();await w.login(foreign,'other-owner')
        for path in ['/v1/dashboard/state','/v1/memory/memories','/v1/control/inbox']:
            require_equal((await foreign.get(path)).status,401,path)
        session=await (await w.client.get('/v1/browser/session')).json()
        await w.sessions.revoke(session['session_id'],principal='local-owner')
        require_equal((await w.client.get('/v1/memory/memories')).status,401)


async def t_task_list_filters_before_limit_and_uses_the_public_result_projection():
    async with world() as w:
        accepted=await (await w.start()).json()
        # More than one page of other principals' tasks must not hide this one.
        for n in range(30):
            w.orch.create_task(objective=f'Other private task {n}',scope='research',origin='local_owner',principal='someone-else')
        rows=await (await w.client.get('/v1/agent/runs')).json()
        require_equal(len(rows['laeufe']),1)
        row=rows['laeufe'][0]
        require_equal(row['id'],accepted['run_id'])
        require_equal(row['kennung'],row['id'])
        require_equal(row['auftrag'],BODY['objective'])
        for key in ['quellen','befunde','pruefung','anbieter','abrechnung','offen']:
            require(key in row,key)
        foreign_id=next(r.run_id for r in w.ledger.recent_runs() if r.run_id!=row['id'])
        require_equal((await w.client.get('/v1/agent/runs/'+foreign_id)).status,404)


async def t_snapshot_does_not_probe_providers_or_turn_unknown_costs_into_zero():
    async with world() as w:
        data=await (await w.client.get('/v1/dashboard/state')).json()
        require_equal(data['environment'],'isolated_test')
        require_equal(data['cost_controls'],'unavailable')
        require_equal(data['quota']['percent_remaining'],None)
        require_equal(data['components'][0]['geprueft_um'],None)
        require_equal(data['components'][0]['zustand'],'unknown')
        require_equal(w.ledger.recent_runs(),[])
        require_equal(await w.memory.semantic.memory.active_records(),[])


async def t_learning_holds_are_read_only_and_filtered_to_the_authenticated_owner():
    from solvio.agent_runtime.cost_subjects import ActivityLedger, _verified_source
    async with world() as w:
        activities = ActivityLedger(w.ledger)
        w.orch.memory_observations = SimpleNamespace(activities=activities, adaptive=w.adaptive)
        for principal in ('local-owner', 'another-owner'):
            source = _verified_source(principal=principal, source_kind='app', source_ref='test-source:'+principal,
                                      conversation_id='test-conversation:'+principal, message_id='test-message')
            binding = activities.admit(source, content_digest='a'*64)
            activities.hold(binding.activity_id, 'extractor_interrupted')
        data = await (await w.client.get('/v1/dashboard/state')).json()
        require_equal(data['learning']['state'], 'enabled')
        require_equal(data['learning']['counts'], {'held': 1})
        require_equal(len(data['learning']['activities']), 1)
        require_equal(data['learning']['activities'][0]['held_reason'], 'extractor_interrupted')
        again = await (await w.client.get('/v1/dashboard/state')).json()
        require_equal(again['learning']['activities'], data['learning']['activities'])
        require_equal(await w.memory.semantic.memory.active_records(), [])


async def t_owner_resumes_one_existing_task_observation_without_reopening_authority():
    from solvio.memory.adaptive.observations import AdaptiveObservations, observation_digest
    from solvio.memory.adaptive.extractor import ExtractionResult
    async with world() as w:
        accepted = await (await w.start()).json()
        observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='local-owner')
        w.orch.memory_observations = observations
        turn, source = observations._task_observation(accepted['task_id'], accepted['run_id'])
        binding = observations.activities.admit(source, content_digest=observation_digest(turn),
            task_id=accepted['task_id'], run_id=accepted['run_id'])
        observations.activities.hold(binding.activity_id, 'extractor_interrupted')
        calls = []
        async def propose(*args, **kwargs):
            calls.append(1); return ExtractionResult(reason='nothing_usable')
        w.adaptive.extractor = SimpleNamespace(propose=propose, last_status={'state':'completed'})
        path = '/v1/dashboard/learning/'+binding.activity_id+'/resume'
        require_equal((await w.client.post(path)).status, 401)
        foreign = await w.new_client(); headers = await w.login(foreign, 'another-owner')
        require_equal((await foreign.post(path, headers=headers)).status, 401)
        with patch.object(w.adaptive, 'start', side_effect=RuntimeError('synthetic worker startup failure')):
            refused = await w.client.post(path, headers=w.headers)
        require_equal(refused.status, 409)
        require_equal((await refused.json())['error'], 'observation_queue_unavailable')
        require_equal(w.adaptive._queue.qsize(), 0)
        require_equal(len(observations._queued), 0)
        replies = await asyncio.gather(*(w.client.post(path, headers=w.headers) for _ in range(2)))
        require_equal(sorted(r.status for r in replies), [202, 409])
        await asyncio.wait_for(w.adaptive._queue.join(), 3)
        require_equal(len(calls), 1)
        require_equal(observations.activities.generation(binding.activity_id), 2)
        require_equal(observations.activities.learning_view('local-owner')['counts'], {'completed':1})
        require_equal(len(w.ledger.recent_runs()), 1)
        require_equal((await w.client.post(path, headers=w.headers)).status, 409)


async def t_expired_task_observation_cannot_release_its_hold_from_the_dashboard():
    from solvio.memory.adaptive.observations import AdaptiveObservations, observation_digest
    async with world() as w:
        accepted = await (await w.start()).json()
        observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='local-owner')
        w.orch.memory_observations = observations
        turn, source = observations._task_observation(accepted['task_id'], accepted['run_id'])
        binding = observations.activities.admit(source, content_digest=observation_digest(turn),
            task_id=accepted['task_id'], run_id=accepted['run_id'], lifetime_seconds=.01)
        observations.activities.hold(binding.activity_id, 'extractor_interrupted')
        await asyncio.sleep(.02)
        r = await w.client.post('/v1/dashboard/learning/'+binding.activity_id+'/resume', headers=w.headers)
        require_equal(r.status, 409)
        require_equal(observations.activities.generation(binding.activity_id), 0)
        require_equal(observations.activities.learning_view('local-owner')['activities'][0]['held_reason'], 'extractor_interrupted')


async def t_restart_between_resume_commit_and_enqueue_restores_a_visible_hold():
    from solvio.memory.adaptive.observations import AdaptiveObservations, observation_digest
    async with world() as w:
        accepted = await (await w.start()).json()
        observations = AdaptiveObservations(w.adaptive, w.ledger, owner_principal='local-owner')
        turn, source = observations._task_observation(accepted['task_id'], accepted['run_id'])
        binding = observations.activities.admit(source, content_digest=observation_digest(turn),
            task_id=accepted['task_id'], run_id=accepted['run_id'])
        observations.activities.hold(binding.activity_id, 'extractor_interrupted')
        observations.activities.resume_bound_task(binding.activity_id, 'local-owner', source, observation_digest(turn))
        # The process died here: no offer/enqueue happened. A fresh observation
        # owner sees the durable pending row, not the lost in-memory queue.
        fresh = AdaptiveObservations(w.adaptive, S.AgentRunLedger(w.ledger.path), owner_principal='local-owner')
        w.orch.memory_observations = fresh
        data = await (await w.client.get('/v1/dashboard/state')).json()
        require_equal(data['learning']['counts'], {'held':1})
        require_equal(data['learning']['activities'][0]['held_reason'], 'observation_interrupted')
        require_equal(fresh.activities.generation(binding.activity_id), 1)
        require_equal(len(fresh._queued), 0)
        require_equal(fresh.offer_task(accepted['task_id'], accepted['run_id']), False)


async def t_browser_mutations_keep_csrf_and_do_not_inherit_scheduler_control():
    async with world() as w:
        accepted=await (await w.start()).json()
        path='/v1/agent/runs/'+accepted['run_id']+'/cancel'
        require_equal((await w.client.post(path)).status,401)
        require_equal((await w.client.post('/v1/control/tasks/no-task/run_now',headers=w.headers)).status,401)
        require_equal((await w.client.post('/v1/control/inbox/no-item/read')).status,401)
        require_equal((await w.client.post(path,headers=w.headers)).status,200)
        require_equal(w.ledger.get_run(accepted['run_id']).state,S.CANCELLED)


async def t_audio_is_read_only_private_and_preserves_unknown_and_observation_age():
    async with world() as w:
        observer=w.voice.audio_observations
        observer.connect('pi-test','session-test','voice_satellite')
        before=observer.snapshot()['devices']
        for _ in range(2):
            r=await w.client.get('/v1/control/audio')
            require_equal(r.status,200)
            data=await r.json()
            require_equal(data['devices'],before)
            require_equal(data['devices'][0]['capture'],'unknown')
        guest=await w.new_client()
        require_equal((await guest.get('/v1/control/audio')).status,401)
        await w.login(guest,'other-owner')
        require_equal((await guest.get('/v1/control/audio')).status,401)
        require_equal(w.ledger.recent_runs(),[])


if __name__ == '__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
